#include "JwtAuthFilter.h"

#include <drogon/HttpClient.h>
#include <drogon/HttpTypes.h>
#include <drogon/drogon.h>

namespace {

void unauthorized(drogon::FilterCallback &fcb, const std::string &message)
{
    Json::Value error;
    error["error"] = message;
    auto resp = drogon::HttpResponse::newHttpJsonResponse(error);
    resp->setStatusCode(drogon::k401Unauthorized);
    fcb(resp);
}

}  // namespace

void JwtAuthFilter::doFilter(const HttpRequestPtr &req,
                             FilterCallback &&fcb,
                             FilterChainCallback &&fccb)
{
    const std::string header = req->getHeader("authorization");
    if (header.empty()) {
        unauthorized(fcb, "Missing Authorization header");
        return;
    }
    if (header.rfind("Bearer ", 0) != 0) {
        unauthorized(fcb, "Invalid Authorization header format. Expected: Bearer <token>");
        return;
    }

    auto custom = app().getCustomConfig();
    const std::string baseUrl = custom["auth_service"]["base_url"].asString();
    const std::string verifyPath = custom["auth_service"]["verify_path"].asString();

    auto client = HttpClient::newHttpClient(baseUrl);
    auto verify = HttpRequest::newHttpRequest();
    verify->setMethod(Get);
    verify->setPath(verifyPath);
    // Never logged: this header is the credential.
    verify->addHeader("Authorization", header);

    client->sendRequest(
        verify,
        [req, fcb = std::move(fcb), fccb = std::move(fccb)](ReqResult result,
                                                            const HttpResponsePtr &resp) mutable {
            if (result != ReqResult::Ok || !resp) {
                // auth-service unreachable is NOT 401. A caller told their
                // credential is bad will go and get a new one; what they
                // actually need is to try again.
                Json::Value error;
                error["error"] = "Authentication service unavailable";
                auto r = HttpResponse::newHttpJsonResponse(error);
                r->setStatusCode(k503ServiceUnavailable);
                r->addHeader("Retry-After", "5");
                fcb(r);
                return;
            }
            if (resp->getStatusCode() != k200OK) {
                unauthorized(fcb, "Unauthorized");
                return;
            }

            auto body = resp->getJsonObject();
            if (!body || !(*body).isMember("user_id") || !(*body)["user_id"].isIntegral()) {
                unauthorized(fcb, "Invalid token payload");
                return;
            }
            req->attributes()->insert("user_id", (*body)["user_id"].asInt());
            fccb();
        });
}
