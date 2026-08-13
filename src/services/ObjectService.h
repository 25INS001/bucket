#pragma once

// ObjectService — the object metadata this service adds on top of S3.
//
// SeaweedFS stores the bytes. What it does not do is version a *logical
// filename*: every upload here gets a random key (objects/<uuid>.<ext>), and
// this table is what maps "the third version of firmware.bin in bucket X" back
// to the key holding it. That mapping, and the "give me the latest" lookup, are
// the reason this service exists at all.
//
// Ported from storage/models.py + storage/views.py. Behaviour is deliberately
// preserved, including the version-allocation race fix — see nextVersion().

#include <drogon/orm/DbClient.h>

#include <optional>
#include <string>

using namespace drogon::orm;

struct ObjectMetadata {
    std::string id;  // uuid
    std::string bucket;
    std::string objectKey;
    std::string originalFilename;
    int version = 0;
    std::optional<long long> size;
    std::optional<std::string> contentType;
    std::optional<std::string> checksum;
    bool isUploaded = false;
    std::string createdAt;
};

class ObjectService {
public:
    explicit ObjectService(DbClientPtr dbClient);

    // Allocates the next version for (bucket, filename) and inserts the row.
    //
    // The read and the write must be one transaction. In the Python original
    // the SELECT MAX(version) sat inside atomic() and the create() that used
    // its result sat outside, so two concurrent uploads of the same file read
    // the same maximum, computed the same next version, and the second violated
    // the unique constraint — surfacing as a 500 on a perfectly valid request.
    //
    // SELECT ... FOR UPDATE locks the existing rows for this (bucket, filename)
    // so the second transaction waits and then reads the first one's row. The
    // retry loop covers the remaining gap: with no rows yet there is nothing to
    // lock, so two uploads of a brand-new filename can still race, and the
    // loser sees a unique violation rather than a wrong answer.
    //
    // (The Python version also needed the retry because SQLite ignores
    // select_for_update. Against PostgreSQL the lock is real, but a first
    // upload still has no row to take, so the loop stays.)
    std::optional<ObjectMetadata> createNextVersion(const std::string& bucket,
                                                    const std::string& filename,
                                                    const std::string& objectKey,
                                                    const std::optional<std::string>& contentType,
                                                    const std::optional<long long>& size,
                                                    int attempts = 5);

    std::optional<ObjectMetadata> findById(const std::string& id);

    // Highest version for (bucket, filename), or nullopt when there is none.
    std::optional<ObjectMetadata> findLatest(const std::string& bucket,
                                             const std::string& filename);

    // A random object key, so the stored key never reveals the original
    // filename and two uploads of the same name never collide.
    static std::string generateObjectKey(const std::string& filename);

private:
    DbClientPtr db;
    static ObjectMetadata rowToObject(const Row& row);
};
