#include "S3Presigner.h"

#include <openssl/evp.h>
#include <openssl/hmac.h>

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <iomanip>
#include <sstream>
#include <vector>

namespace s3 {

namespace {

constexpr const char* kAlgorithm = "AWS4-HMAC-SHA256";
constexpr const char* kService = "s3";
// A presigned URL is redeemed by a client whose body this service never sees,
// so the payload cannot be hashed at signing time.
constexpr const char* kUnsignedPayload = "UNSIGNED-PAYLOAD";

std::string toHex(const unsigned char* data, size_t len)
{
    std::ostringstream oss;
    for (size_t i = 0; i < len; ++i) {
        oss << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(data[i]);
    }
    return oss.str();
}

std::string hmacRaw(const std::string& key, const std::string& data)
{
    unsigned char out[EVP_MAX_MD_SIZE];
    unsigned int len = 0;
    HMAC(EVP_sha256(),
         key.data(), static_cast<int>(key.size()),
         reinterpret_cast<const unsigned char*>(data.data()), data.size(),
         out, &len);
    return std::string(reinterpret_cast<char*>(out), len);
}

// Splits "http://host:port" into scheme, host[:port]. Host keeps the port
// because that is what goes in the Host header, and therefore what is signed.
bool splitEndpoint(const std::string& endpoint, std::string& scheme, std::string& host)
{
    const auto sep = endpoint.find("://");
    if (sep == std::string::npos) return false;
    scheme = endpoint.substr(0, sep);
    host = endpoint.substr(sep + 3);
    while (!host.empty() && host.back() == '/') host.pop_back();
    return !scheme.empty() && !host.empty();
}

std::string utcNow()
{
    const std::time_t now = std::time(nullptr);
    std::tm tm{};
    gmtime_r(&now, &tm);
    char buf[32];
    std::strftime(buf, sizeof(buf), "%Y%m%dT%H%M%SZ", &tm);
    return buf;
}

}  // namespace

std::string uriEncode(const std::string& input, bool encodeSlash)
{
    std::ostringstream out;
    for (unsigned char c : input) {
        const bool unreserved = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                                (c >= '0' && c <= '9') ||
                                c == '-' || c == '_' || c == '.' || c == '~';
        if (unreserved) {
            out << static_cast<char>(c);
        } else if (c == '/' && !encodeSlash) {
            out << '/';
        } else {
            // Uppercase hex is required; lowercase produces a valid-looking URL
            // that fails to verify.
            out << '%' << std::uppercase << std::hex << std::setw(2) << std::setfill('0')
                << static_cast<int>(c) << std::nouppercase << std::dec;
        }
    }
    return out.str();
}

std::string sha256Hex(const std::string& input)
{
    unsigned char hash[EVP_MAX_MD_SIZE];
    unsigned int len = 0;
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr);
    EVP_DigestUpdate(ctx, input.data(), input.size());
    EVP_DigestFinal_ex(ctx, hash, &len);
    EVP_MD_CTX_free(ctx);
    return toHex(hash, len);
}

std::string hmacHex(const std::string& key, const std::string& data)
{
    const std::string raw = hmacRaw(key, data);
    return toHex(reinterpret_cast<const unsigned char*>(raw.data()), raw.size());
}

std::string signingKey(const std::string& secretKey,
                       const std::string& dateStamp,
                       const std::string& region,
                       const std::string& service)
{
    const std::string kDate = hmacRaw("AWS4" + secretKey, dateStamp);
    const std::string kRegion = hmacRaw(kDate, region);
    const std::string kService = hmacRaw(kRegion, service);
    return hmacRaw(kService, "aws4_request");
}

std::string presign(const PresignRequest& request,
                    const Credentials& credentials,
                    const std::string& amzDateOverride)
{
    std::string scheme, host;
    if (!splitEndpoint(request.endpoint, scheme, host)) return "";
    if (request.bucket.empty() || request.key.empty()) return "";

    const std::string amzDate = amzDateOverride.empty() ? utcNow() : amzDateOverride;
    if (amzDate.size() < 8) return "";
    const std::string dateStamp = amzDate.substr(0, 8);  // YYYYMMDD

    // Path-style addressing: /<bucket>/<key>. Virtual-hosted style would put the
    // bucket in the host instead; SeaweedFS is configured for path-style, and
    // the caller supplies the endpoint, so this stays consistent with it.
    //
    // The bucket and key are encoded separately, then joined, so that a key
    // containing "/" keeps its slashes as path separators.
    const std::string canonicalUri =
        "/" + uriEncode(request.bucket, false) + "/" + uriEncode(request.key, false);

    const std::string credentialScope =
        dateStamp + "/" + credentials.region + "/" + kService + "/aws4_request";

    // std::map keeps the query sorted by key, which is what the canonical form
    // requires — SigV4 sorts by the ENCODED name, and every name here is
    // already URL-safe.
    std::map<std::string, std::string> query = request.queryParams;
    query["X-Amz-Algorithm"] = kAlgorithm;
    query["X-Amz-Credential"] = credentials.accessKey + "/" + credentialScope;
    query["X-Amz-Date"] = amzDate;
    query["X-Amz-Expires"] = std::to_string(request.expiresInSeconds);
    query["X-Amz-SignedHeaders"] = "host";

    std::string canonicalQuery;
    for (const auto& [name, value] : query) {
        if (!canonicalQuery.empty()) canonicalQuery += '&';
        canonicalQuery += uriEncode(name, true) + "=" + uriEncode(value, true);
    }

    // Only Host is signed. Anything else would have to be reproduced exactly by
    // the client redeeming the URL, which it has no way to know.
    const std::string canonicalHeaders = "host:" + host + "\n";
    const std::string signedHeaders = "host";

    const std::string canonicalRequest =
        request.method + "\n" +
        canonicalUri + "\n" +
        canonicalQuery + "\n" +
        canonicalHeaders + "\n" +
        signedHeaders + "\n" +
        kUnsignedPayload;

    const std::string stringToSign =
        std::string(kAlgorithm) + "\n" +
        amzDate + "\n" +
        credentialScope + "\n" +
        sha256Hex(canonicalRequest);

    const std::string key = signingKey(credentials.secretKey, dateStamp,
                                       credentials.region, kService);
    const std::string signature = hmacHex(key, stringToSign);

    return scheme + "://" + host + canonicalUri + "?" + canonicalQuery +
           "&X-Amz-Signature=" + signature;
}

}  // namespace s3
