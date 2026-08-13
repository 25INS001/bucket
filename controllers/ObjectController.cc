#include <drogon/HttpController.h>
#include <drogon/drogon.h>

#include "services/ObjectService.h"
#include "services/S3Client.h"
#include "services/S3Presigner.h"

using namespace drogon;

// ObjectController — presigned upload and download, with filename versioning.
//
// Bytes never pass through this service. Both write paths hand back a
// short-lived presigned URL and the client talks to S3 directly, which is why
// a C++ port is a straight swap: there was never any streaming to reimplement.
//
// AUTHENTICATION. The Python service had none — plain APIView subclasses, no
// permission_classes, no REST_FRAMEWORK block, so DRF defaulted to AllowAny
// while nginx exposed /bucket/ publicly. Anyone who could reach the host could
// mint a presigned PUT into any bucket name they chose. Every route here sits
// behind JwtAuthFilter; fixing it during the port costs one argument per route,
// and leaving it would have carried an open write surface into a new service.
class ObjectController : public drogon::HttpController<ObjectController>
{
public:
    METHOD_LIST_BEGIN
    ADD_METHOD_TO(ObjectController::createObject, "/api/objects/", Post, "JwtAuthFilter");
    ADD_METHOD_TO(ObjectController::getLatest, "/api/objects/latest/", Get, "JwtAuthFilter");
    ADD_METHOD_TO(ObjectController::getObject, "/api/objects/{1}/", Get, "JwtAuthFilter");
    METHOD_LIST_END

    // POST /api/objects/  {bucket, filename, content_type?, size?}
    //   -> 200 {object_id, upload_url}
    void createObject(const HttpRequestPtr &req,
                      std::function<void(const HttpResponsePtr &)> &&callback)
    {
        auto jsonPtr = req->getJsonObject();
        const Json::Value body = jsonPtr ? *jsonPtr : Json::Value(Json::objectValue);

        const std::string bucket = requiredString(body, "bucket");
        const std::string filename = requiredString(body, "filename");
        if (bucket.empty() || filename.empty()) {
            return fail(callback, k400BadRequest, "bucket and filename are required");
        }

        std::optional<std::string> contentType;
        if (body.isMember("content_type") && body["content_type"].isString() &&
            !body["content_type"].asString().empty()) {
            contentType = body["content_type"].asString();
        }

        // Present-but-wrong is rejected rather than ignored: a size of -1 or
        // "12" means the caller believes something this service does not.
        std::optional<long long> size;
        if (body.isMember("size") && !body["size"].isNull()) {
            if (!body["size"].isIntegral() || body["size"].asInt64() < 0) {
                return fail(callback, k400BadRequest, "size must be a non-negative integer");
            }
            size = body["size"].asInt64();
        }

        const std::string objectKey = ObjectService::generateObjectKey(filename);
        if (objectKey.empty()) {
            return fail(callback, k500InternalServerError, "Could not generate an object key");
        }

        ObjectService objects(app().getDbClient("default"));
        auto stored = objects.createNextVersion(bucket, filename, objectKey, contentType, size);
        if (!stored) {
            return fail(callback, k500InternalServerError, "Could not allocate an object version");
        }

        const PublicEndpoint pub = publicEndpoint();
        s3::PresignRequest presign;
        presign.method = "PUT";
        presign.endpoint = pub.origin;
        presign.pathPrefix = pub.prefix;
        presign.bucket = bucket;
        presign.key = objectKey;
        presign.expiresInSeconds = kUrlTtlSeconds;
        // Signed so S3 rejects an upload that arrives as a different type than
        // the one recorded here.
        if (contentType) presign.queryParams["Content-Type"] = *contentType;

        const std::string url = s3::presign(presign, credentials());
        if (url.empty()) {
            return fail(callback, k500InternalServerError, "Could not sign the upload URL");
        }

        Json::Value out;
        out["object_id"] = stored->id;
        out["upload_url"] = url;
        callback(HttpResponse::newHttpJsonResponse(out));
    }

    // GET /api/objects/{id}/  -> 200 {download_url}
    void getObject(const HttpRequestPtr &req,
                   std::function<void(const HttpResponsePtr &)> &&callback,
                   const std::string &objectId)
    {
        ObjectService objects(app().getDbClient("default"));
        auto stored = objects.findById(objectId);
        // A malformed uuid fails the cast and lands here too, which is right:
        // an id that cannot exist is not found.
        if (!stored) return fail(callback, k404NotFound, "Not found");

        // Whether the upload actually happened is asked of S3, not read from
        // is_uploaded. Nothing has ever written that column — there is no
        // completion callback — so trusting it refused every download. Asking
        // the store cannot drift: an object deleted afterwards stops being
        // downloadable at the right moment, and one uploaded by a path this
        // service never saw still works. See S3Client.h.
        s3::PresignRequest probe;
        probe.method = "HEAD";
        probe.endpoint = endpoint();
        probe.bucket = stored->bucket;
        probe.key = stored->objectKey;
        probe.expiresInSeconds = 60;  // used immediately, never handed out

        switch (s3::headObject(s3::presign(probe, credentials()))) {
            case s3::Existence::Present:
                break;
            case s3::Existence::Absent:
                // The row exists because an upload was requested; the bytes
                // never arrived, or were removed since.
                return fail(callback, k404NotFound, "Upload not completed");
            case s3::Existence::Unavailable:
                // Not 404: telling a client its object is missing because the
                // store was briefly unreachable invites it to re-upload
                // something that is already there.
                return fail(callback, k503ServiceUnavailable,
                            "Object store unavailable; try again shortly");
        }

        const PublicEndpoint pub = publicEndpoint();
        s3::PresignRequest presign;
        presign.method = "GET";
        presign.endpoint = pub.origin;
        presign.pathPrefix = pub.prefix;
        presign.bucket = stored->bucket;
        presign.key = stored->objectKey;
        presign.expiresInSeconds = kUrlTtlSeconds;
        // Restores the name the uploader used — the stored key is a uuid, so
        // without this every download arrives called something meaningless.
        presign.queryParams["response-content-disposition"] =
            "attachment; filename=\"" + sanitiseFilename(stored->originalFilename) + "\"";

        const std::string url = s3::presign(presign, credentials());
        if (url.empty()) {
            return fail(callback, k500InternalServerError, "Could not sign the download URL");
        }

        Json::Value out;
        out["download_url"] = url;
        callback(HttpResponse::newHttpJsonResponse(out));
    }

    // GET /api/objects/latest/?bucket=&filename=
    void getLatest(const HttpRequestPtr &req,
                   std::function<void(const HttpResponsePtr &)> &&callback)
    {
        const auto &params = req->getParameters();
        const auto bucket = params.find("bucket");
        const auto filename = params.find("filename");
        if (bucket == params.end() || filename == params.end() ||
            bucket->second.empty() || filename->second.empty()) {
            return fail(callback, k400BadRequest, "bucket and filename are required");
        }

        ObjectService objects(app().getDbClient("default"));
        auto stored = objects.findLatest(bucket->second, filename->second);
        if (!stored) return fail(callback, k404NotFound, "File not found");

        Json::Value out;
        out["object_id"] = stored->id;
        out["bucket"] = stored->bucket;
        out["filename"] = stored->originalFilename;
        out["version"] = stored->version;

        // download_url is part of this endpoint's contract — the Python service
        // returned it and callers use it to fetch in one round trip instead of
        // two.
        //
        // It did so unconditionally, including for a version that was only ever
        // registered, which handed out a signed link to nothing. Here the same
        // existence check as getObject() decides: present means a URL, absent
        // means the metadata without one and downloadable=false, so a caller
        // can tell "no such file" from "the newest version was never uploaded".
        const s3::Existence present = s3::headObject(
            [&] {
                s3::PresignRequest probe;
                probe.method = "HEAD";
                probe.endpoint = endpoint();
                probe.bucket = stored->bucket;
                probe.key = stored->objectKey;
                probe.expiresInSeconds = 60;
                return s3::presign(probe, credentials());
            }());

        if (present == s3::Existence::Unavailable) {
            return fail(callback, k503ServiceUnavailable,
                        "Object store unavailable; try again shortly");
        }
        out["downloadable"] = (present == s3::Existence::Present);
        if (present == s3::Existence::Present) {
            const PublicEndpoint pub = publicEndpoint();
            s3::PresignRequest download;
            download.method = "GET";
            download.endpoint = pub.origin;
            download.pathPrefix = pub.prefix;
            download.bucket = stored->bucket;
            download.key = stored->objectKey;
            download.expiresInSeconds = kUrlTtlSeconds;
            download.queryParams["response-content-disposition"] =
                "attachment; filename=\"" + sanitiseFilename(stored->originalFilename) + "\"";
            const std::string url = s3::presign(download, credentials());
            if (url.empty()) {
                return fail(callback, k500InternalServerError,
                            "Could not sign the download URL");
            }
            out["download_url"] = url;
        }
        // Reported for continuity with the previous API. It is NOT what
        // gates downloads any more — nothing writes it, so it is always
        // false. The download path asks S3 instead.
        out["is_uploaded"] = stored->isUploaded;
        out["created_at"] = stored->createdAt;
        if (stored->size) out["size"] = static_cast<Json::Int64>(*stored->size);
        if (stored->contentType) out["content_type"] = *stored->contentType;
        callback(HttpResponse::newHttpJsonResponse(out));
    }

private:
    static constexpr int kUrlTtlSeconds = 300;

    static std::string requiredString(const Json::Value &body, const char *name)
    {
        if (!body.isMember(name) || !body[name].isString()) return "";
        return body[name].asString();
    }

    // A filename reaches the client inside a quoted Content-Disposition header.
    // A quote or a newline in it would end the quoted string early and let the
    // rest be read as further header content.
    static std::string sanitiseFilename(const std::string &name)
    {
        std::string out;
        out.reserve(name.size());
        for (char c : name) {
            if (c == '"' || c == '\\' || c == '\r' || c == '\n') continue;
            out += c;
        }
        return out.empty() ? "download" : out;
    }

    // Where THIS SERVICE reaches the object store: an in-network hostname, no
    // proxy in between.
    static std::string endpoint()
    {
        auto custom = app().getCustomConfig();
        return custom["s3"]["endpoint"].asString();
    }

    // Where a CLIENT reaches it. Usually the public host, with the store
    // published under a path prefix that nginx strips before proxying.
    //
    // These have to be separate. A URL signed for "http://s3:8333" is
    // unresolvable outside the compose network, so a browser handed one cannot
    // fetch anything — which is the state the Python service shipped in.
    // Falls back to the internal endpoint when unset, which is the old
    // behaviour and correct for a client that is itself in-network.
    struct PublicEndpoint {
        std::string origin;  // scheme://host[:port]
        std::string prefix;  // path nginx strips, e.g. "/s3"
    };

    static PublicEndpoint publicEndpoint()
    {
        auto custom = app().getCustomConfig();
        std::string url = custom["s3"]["public_url"].asString();
        if (url.empty()) return {endpoint(), ""};

        const auto sep = url.find("://");
        if (sep == std::string::npos) return {endpoint(), ""};
        const auto slash = url.find('/', sep + 3);
        if (slash == std::string::npos) return {url, ""};
        return {url.substr(0, slash), url.substr(slash)};
    }

    static s3::Credentials credentials()
    {
        auto custom = app().getCustomConfig();
        return s3::Credentials{
            custom["s3"]["access_key"].asString(),
            custom["s3"]["secret_key"].asString(),
            custom["s3"]["region"].asString(),
        };
    }

    static void fail(const std::function<void(const HttpResponsePtr &)> &callback,
                     HttpStatusCode code,
                     const std::string &message)
    {
        Json::Value error;
        error["error"] = message;
        auto resp = HttpResponse::newHttpJsonResponse(error);
        resp->setStatusCode(code);
        // A 503 here means "ask again", so say when. Without it a client has to
        // guess, and the usual guess is either immediately or never.
        if (code == k503ServiceUnavailable) resp->addHeader("Retry-After", "5");
        callback(resp);
    }
};
