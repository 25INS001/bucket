#include "ObjectService.h"

#include <drogon/orm/Exception.h>
#include <openssl/rand.h>

#include <iostream>

namespace {

// A v4 UUID from the CSPRNG. std::random_device is not guaranteed to be one,
// and this value is the only thing standing between an object key and being
// guessable.
std::string uuidV4()
{
    unsigned char b[16];
    if (RAND_bytes(b, sizeof(b)) != 1) return "";

    b[6] = static_cast<unsigned char>((b[6] & 0x0F) | 0x40);  // version 4
    b[8] = static_cast<unsigned char>((b[8] & 0x3F) | 0x80);  // variant 1

    static const char* hex = "0123456789abcdef";
    std::string out;
    out.reserve(36);
    for (int i = 0; i < 16; ++i) {
        if (i == 4 || i == 6 || i == 8 || i == 10) out += '-';
        out += hex[b[i] >> 4];
        out += hex[b[i] & 0x0F];
    }
    return out;
}

// The extension only, lowercased by nobody — kept exactly as the Python version
// produced it, so keys generated before and after the port look the same.
std::string extensionOf(const std::string& filename)
{
    const auto dot = filename.rfind('.');
    if (dot == std::string::npos || dot + 1 >= filename.size()) return "";
    return filename.substr(dot + 1);
}

}  // namespace

ObjectService::ObjectService(DbClientPtr dbClient) : db(std::move(dbClient)) {}

std::string ObjectService::generateObjectKey(const std::string& filename)
{
    const std::string uid = uuidV4();
    if (uid.empty()) return "";
    const std::string ext = extensionOf(filename);
    return ext.empty() ? "objects/" + uid : "objects/" + uid + "." + ext;
}

ObjectMetadata ObjectService::rowToObject(const Row& row)
{
    ObjectMetadata o;
    o.id = row["id"].as<std::string>();
    o.bucket = row["bucket"].as<std::string>();
    o.objectKey = row["object_key"].as<std::string>();
    o.originalFilename = row["original_filename"].as<std::string>();
    o.version = row["version"].as<int>();
    if (!row["size"].isNull()) o.size = row["size"].as<long long>();
    if (!row["content_type"].isNull()) o.contentType = row["content_type"].as<std::string>();
    if (!row["checksum"].isNull()) o.checksum = row["checksum"].as<std::string>();
    o.isUploaded = row["is_uploaded"].as<bool>();
    o.createdAt = row["created_at"].as<std::string>();
    return o;
}

std::optional<ObjectMetadata> ObjectService::createNextVersion(
    const std::string& bucket,
    const std::string& filename,
    const std::string& objectKey,
    const std::optional<std::string>& contentType,
    const std::optional<long long>& size,
    int attempts)
{
    for (int attempt = 0; attempt < attempts; ++attempt) {
        try {
            auto trans = db->newTransaction();

            // FOR UPDATE locks whatever rows exist for this (bucket, filename).
            // A concurrent upload of the same name blocks here and then reads
            // the row this one is about to write.
            //
            // The lock has to be taken in a subquery: PostgreSQL rejects
            // "FOR UPDATE is not allowed with aggregate functions", so
            // SELECT MAX(version) ... FOR UPDATE fails outright. Locking the
            // rows first and aggregating over the result keeps both.
            auto current = trans->execSqlSync(
                "SELECT COALESCE(MAX(version), 0) AS v FROM ("
                "    SELECT version FROM object_metadata "
                "    WHERE bucket = $1 AND original_filename = $2 "
                "    FOR UPDATE"
                ") locked",
                bucket,
                filename
            );
            const int next = (current.empty() ? 0 : current[0]["v"].as<int>()) + 1;

            auto inserted = trans->execSqlSync(
                "INSERT INTO object_metadata "
                "(bucket, object_key, original_filename, version, content_type, size) "
                "VALUES ($1, $2, $3, $4::int, $5, $6) "
                "RETURNING id::text, bucket, object_key, original_filename, version, "
                "          size, content_type, checksum, is_uploaded, created_at",
                bucket,
                objectKey,
                filename,
                next,
                contentType,
                size
            );
            if (inserted.empty()) {
                trans->rollback();
                return std::nullopt;
            }
            return rowToObject(inserted[0]);
        }
        catch (const DrogonDbException& e) {
            // A unique violation means someone else took this version number
            // between the aggregate and the insert — read again and take the
            // next one. Any other failure is not retryable.
            const std::string what = e.base().what();
            const bool raced = what.find("duplicate key") != std::string::npos ||
                               what.find("unique constraint") != std::string::npos;
            if (!raced || attempt == attempts - 1) {
                std::cerr << "Error allocating object version: " << what << std::endl;
                return std::nullopt;
            }
        }
    }
    return std::nullopt;
}

std::optional<ObjectMetadata> ObjectService::findById(const std::string& id)
{
    try {
        auto result = db->execSqlSync(
            "SELECT id::text, bucket, object_key, original_filename, version, size, "
            "       content_type, checksum, is_uploaded, created_at "
            "FROM object_metadata WHERE id = $1::uuid",
            id
        );
        if (result.empty()) return std::nullopt;
        return rowToObject(result[0]);
    }
    catch (const DrogonDbException& e) {
        // Includes a malformed uuid, which Postgres rejects on the cast. The
        // caller reports that as "not found" rather than as a server error: a
        // client that sends a bad id is asking about something that cannot
        // exist.
        std::cerr << "Error fetching object: " << e.base().what() << std::endl;
        return std::nullopt;
    }
}

std::optional<ObjectMetadata> ObjectService::findLatest(const std::string& bucket,
                                                        const std::string& filename)
{
    try {
        auto result = db->execSqlSync(
            "SELECT id::text, bucket, object_key, original_filename, version, size, "
            "       content_type, checksum, is_uploaded, created_at "
            "FROM object_metadata WHERE bucket = $1 AND original_filename = $2 "
            "ORDER BY version DESC LIMIT 1",
            bucket,
            filename
        );
        if (result.empty()) return std::nullopt;
        return rowToObject(result[0]);
    }
    catch (const DrogonDbException& e) {
        std::cerr << "Error fetching latest object: " << e.base().what() << std::endl;
        return std::nullopt;
    }
}
