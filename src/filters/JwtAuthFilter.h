#pragma once

// JwtAuthFilter — verifies the bearer token with auth-service.
//
// bucket-service has no user table and no JWT secret; it asks auth-service
// whether a token is valid, exactly as blob-service and hfrw-service do.
//
// Unlike hfrw-service's AuthFilter, this KEEPS the verify response and
// publishes user_id into the request attributes. Discarding it is how hfrw
// ended up establishing that a caller holds *a* valid token but never *whose*,
// which is why its handlers trust user_id from the request body.

#include <drogon/HttpFilter.h>

using namespace drogon;

class JwtAuthFilter : public HttpFilter<JwtAuthFilter>
{
public:
    void doFilter(const HttpRequestPtr &req,
                  FilterCallback &&fcb,
                  FilterChainCallback &&fccb) override;
};
