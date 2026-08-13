#pragma once

// S3Client — asks the object store whether an object is actually there.
//
// The metadata row is created when an upload is *requested*, and the client
// then PUTs directly to S3 without telling this service whether it succeeded.
// The `is_uploaded` column was meant to record that, but nothing ever set it —
// not here and not in the Python service it was ported from — so the download
// path refused every request it was ever given.
//
// Rather than reintroduce a flag that has to be kept in step with reality by
// something remembering to write it, the download path asks S3 directly. The
// answer cannot drift: an object that was uploaded and later deleted stops
// being downloadable at exactly the right moment, and one uploaded through some
// path this service never saw still works.
//
// The cost is one HEAD per download request, on the same network as the S3
// gateway, before a URL is handed out.
//
// libcurl rather than drogon's HttpClient on purpose: the presigned URL is
// already fully encoded and its signature covers that exact byte sequence.
// curl sends the string verbatim; re-assembling it from path plus parameters
// risks re-encoding a value and invalidating the signature.

#include <string>

namespace s3 {

enum class Existence {
    Present,
    Absent,       // the store answered, and there is no such object
    Unavailable,  // the store could not be reached or refused the question
};

// Issues a HEAD against an already-signed URL.
//
// Unavailable is kept apart from Absent because they mean opposite things to a
// caller: one is "this object is not there", the other is "ask again later".
// Reporting an outage as a missing object would tell a client its upload was
// lost.
Existence headObject(const std::string& presignedUrl, long timeoutMs = 2000);

}  // namespace s3
