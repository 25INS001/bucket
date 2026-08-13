#pragma once

// S3Presigner — AWS Signature Version 4, query-string ("presigned URL") flavour.
//
// This is the one thing boto3 was doing that has no Drogon equivalent, and the
// only reason this service exists: bytes never pass through it, it hands out
// short-lived URLs and gets out of the way.
//
// Hand-rolled rather than pulled from aws-sdk-cpp. The signing recipe is fixed,
// self-contained and testable offline against AWS's published vectors, whereas
// the SDK would roughly double an already-large build image for one function.
//
// The algorithm, in order:
//
//   1. canonical request  method, URI-encoded path, sorted query, signed
//                         headers, and UNSIGNED-PAYLOAD (a presigned URL is
//                         handed to a client whose body we never see)
//   2. string to sign     algorithm, timestamp, credential scope, and the
//                         SHA-256 of (1)
//   3. signing key        HMAC chain over date -> region -> service ->
//                         "aws4_request", starting from "AWS4" + secret
//   4. signature          HMAC of (2) with (3), hex, appended as X-Amz-Signature
//
// Two details cause most SigV4 failures, and both are handled in uriEncode():
// the path encodes every character except unreserved and "/", while query
// components encode "/" as well; and the hex must be uppercase.

#include <map>
#include <string>

namespace s3 {

struct Credentials {
    std::string accessKey;
    std::string secretKey;
    std::string region;
};

struct PresignRequest {
    std::string method;    // "GET", "PUT" or "HEAD"
    std::string endpoint;  // e.g. "http://s3:8333" — scheme://host[:port]
    std::string bucket;
    std::string key;
    int expiresInSeconds = 300;

    // Emitted in the URL but deliberately NOT signed.
    //
    // A URL handed to a browser goes through nginx, which publishes the store
    // under a prefix ("/s3") and strips it before proxying. So the client
    // requests /s3/<bucket>/<key> while the store sees /<bucket>/<key> — and
    // the signature has to match what the STORE sees, not what the client
    // sent. Signing the prefix would fail verification at the far end; omitting
    // it from the URL would miss nginx's location entirely.
    //
    // Empty for the internal endpoint, where there is no proxy in between.
    std::string pathPrefix;

    // Extra query parameters signed into the URL. Used for
    // response-content-disposition on download and Content-Type on upload.
    std::map<std::string, std::string> queryParams;
};

// Builds a presigned URL. Empty only if the request is unusable (no endpoint,
// bucket or key), which the caller should treat as a configuration fault.
//
// `now` is injected rather than read from the clock so the signature is
// reproducible in tests; pass std::nullopt in production. Format: %Y%m%dT%H%M%SZ.
std::string presign(const PresignRequest& request,
                    const Credentials& credentials,
                    const std::string& amzDateOverride = "");

// --- exposed for testing --------------------------------------------------

// Percent-encoding per SigV4. `encodeSlash` is false for the path component
// and true everywhere else.
std::string uriEncode(const std::string& input, bool encodeSlash);

std::string sha256Hex(const std::string& input);

// The date -> region -> service -> aws4_request HMAC chain.
std::string signingKey(const std::string& secretKey,
                       const std::string& dateStamp,
                       const std::string& region,
                       const std::string& service);

std::string hmacHex(const std::string& key, const std::string& data);

}  // namespace s3
