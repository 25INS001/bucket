#include "S3Client.h"

#include <curl/curl.h>

#include <iostream>
#include <mutex>

namespace s3 {

namespace {

void ensureGlobalInit()
{
    static std::once_flag once;
    std::call_once(once, [] { curl_global_init(CURL_GLOBAL_DEFAULT); });
}

}  // namespace

Existence headObject(const std::string& presignedUrl, long timeoutMs)
{
    if (presignedUrl.empty()) return Existence::Unavailable;

    ensureGlobalInit();
    CURL* curl = curl_easy_init();
    if (!curl) return Existence::Unavailable;

    curl_easy_setopt(curl, CURLOPT_URL, presignedUrl.c_str());
    // HEAD: the body is irrelevant, only whether the object is there.
    curl_easy_setopt(curl, CURLOPT_NOBODY, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeoutMs);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    // A redirect would be followed without the signature, which cannot succeed
    // and would only turn a clear answer into a confusing one.
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);

    const CURLcode rc = curl_easy_perform(curl);
    long status = 0;
    if (rc == CURLE_OK) curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_cleanup(curl);

    if (rc != CURLE_OK) {
        std::cerr << "S3 HEAD transport error: " << curl_easy_strerror(rc) << std::endl;
        return Existence::Unavailable;
    }

    if (status == 200) return Existence::Present;
    if (status == 404) return Existence::Absent;

    // 403 lands here deliberately. S3 answers it both for a genuinely missing
    // object (when the caller may not list the bucket) and for a signature the
    // store rejected — and those need opposite responses, so neither is
    // guessed at. Anything else unexpected is treated the same way.
    std::cerr << "S3 HEAD returned unexpected status " << status << std::endl;
    return Existence::Unavailable;
}

}  // namespace s3
