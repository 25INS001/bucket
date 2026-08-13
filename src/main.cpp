#include <drogon/drogon.h>

#include <cstdlib>
#include <iostream>
#include <string>

namespace {

// Fail at startup rather than on the first request. A service that starts
// without its S3 credentials looks healthy and hands out URLs that no client
// can redeem.
bool requireCustom(const Json::Value &custom, const char *section, const char *key)
{
    if (!custom.isMember(section) || !custom[section].isMember(key) ||
        custom[section][key].asString().empty()) {
        std::cerr << "FATAL: custom_config." << section << "." << key
                  << " is empty. It is rendered from the environment by "
                     "docker/entrypoint.sh — check the S3_* variables.\n";
        return false;
    }
    return true;
}

}  // namespace

int main()
{
    drogon::app().loadConfigFile("/app/src/config.json");

    const auto custom = drogon::app().getCustomConfig();
    for (const char *key : {"endpoint", "access_key", "secret_key", "region"}) {
        if (!requireCustom(custom, "s3", key)) return 1;
    }
    if (!requireCustom(custom, "auth_service", "base_url")) return 1;

    std::cout << "bucket-service starting on http://0.0.0.0:8000" << std::endl;
    drogon::app().run();
    return 0;
}
