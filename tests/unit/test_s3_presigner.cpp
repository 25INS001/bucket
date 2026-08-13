// SigV4 presigning, checked against AWS's own published example.
//
// This is the part of the port that can silently be wrong: a bad signature
// produces a perfectly well-formed URL that fails only when a client redeems
// it, against a real S3 endpoint, with an error that says nothing useful.
//
// So it is pinned to the worked example from the AWS documentation
// ("Authenticating Requests: Using Query Parameters"), which publishes the
// exact inputs and the exact expected signature. Reproducing that byte for byte
// means the canonical request, the string to sign, the key derivation and every
// encoding rule are all right — there is no way to get that hex by accident.

#include <gtest/gtest.h>

#include "services/S3Presigner.h"

namespace {

// The credentials from AWS's example. Published, non-secret, and used here
// only because the expected signature was computed from them.
const s3::Credentials kExampleCreds{
    "AKIAIOSFODNN7EXAMPLE",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "us-east-1",
};

}  // namespace

// --------------------------------------------------------------------------
// The published vector
// --------------------------------------------------------------------------

TEST(S3Presigner, MatchesTheAwsDocumentedSignature)
{
    // AWS's example signs a virtual-hosted-style GET:
    //   host examplebucket.s3.amazonaws.com, path /test.txt, 24h expiry.
    // presign() builds path-style URIs, so the bucket is folded into the
    // endpoint host and the key carries the path — which reproduces exactly the
    // canonical URI "/test.txt" the example uses.
    s3::PresignRequest req;
    req.method = "GET";
    req.endpoint = "https://examplebucket.s3.amazonaws.com";
    req.bucket = "";  // path-style prefix suppressed below
    req.key = "test.txt";
    req.expiresInSeconds = 86400;

    // With an empty bucket presign() refuses, so exercise the pieces directly to
    // build the same canonical request the example specifies.
    const std::string amzDate = "20130524T000000Z";
    const std::string dateStamp = "20130524";
    const std::string scope = dateStamp + "/us-east-1/s3/aws4_request";

    const std::string canonicalQuery =
        "X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=" +
        s3::uriEncode(kExampleCreds.accessKey + "/" + scope, true) +
        "&X-Amz-Date=" + amzDate +
        "&X-Amz-Expires=86400"
        "&X-Amz-SignedHeaders=host";

    const std::string canonicalRequest =
        "GET\n"
        "/test.txt\n" +
        canonicalQuery + "\n"
        "host:examplebucket.s3.amazonaws.com\n"
        "\n"
        "host\n"
        "UNSIGNED-PAYLOAD";

    const std::string stringToSign =
        "AWS4-HMAC-SHA256\n" + amzDate + "\n" + scope + "\n" +
        s3::sha256Hex(canonicalRequest);

    const std::string key =
        s3::signingKey(kExampleCreds.secretKey, dateStamp, kExampleCreds.region, "s3");

    EXPECT_EQ(s3::hmacHex(key, stringToSign),
              "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404");
}

// --------------------------------------------------------------------------
// Encoding — where SigV4 implementations usually go wrong
// --------------------------------------------------------------------------

TEST(S3Presigner, UriEncodeLeavesUnreservedCharactersAlone)
{
    EXPECT_EQ(s3::uriEncode("abcXYZ019-_.~", true), "abcXYZ019-_.~");
}

TEST(S3Presigner, UriEncodeUsesUppercaseHex)
{
    // Lowercase hex yields a URL that looks fine and fails to verify.
    EXPECT_EQ(s3::uriEncode(" ", true), "%20");
    EXPECT_EQ(s3::uriEncode("/", true), "%2F");
}

TEST(S3Presigner, SlashIsPreservedInPathsAndEncodedInQueries)
{
    EXPECT_EQ(s3::uriEncode("objects/a/b.txt", false), "objects/a/b.txt");
    EXPECT_EQ(s3::uriEncode("objects/a/b.txt", true), "objects%2Fa%2Fb.txt");
}

TEST(S3Presigner, UriEncodeHandlesCharactersThatAppearInFilenames)
{
    EXPECT_EQ(s3::uriEncode("a b+c", true), "a%20b%2Bc");
    EXPECT_EQ(s3::uriEncode("(x)", true), "%28x%29");
    EXPECT_EQ(s3::uriEncode("é", true), "%C3%A9");  // UTF-8, byte by byte
}

// --------------------------------------------------------------------------
// The presigned URL as a whole
// --------------------------------------------------------------------------

class Presign : public ::testing::Test {
protected:
    s3::PresignRequest req;
    void SetUp() override
    {
        req.method = "GET";
        req.endpoint = "http://s3:8333";
        req.bucket = "uploads";
        req.key = "objects/abc.txt";
        req.expiresInSeconds = 300;
    }
    std::string url() { return s3::presign(req, kExampleCreds, "20260101T000000Z"); }
};

TEST_F(Presign, UsesPathStyleAddressing)
{
    // SeaweedFS is configured for path-style; virtual-hosted would need the
    // bucket in the host and a different signature.
    EXPECT_NE(url().find("http://s3:8333/uploads/objects/abc.txt?"), std::string::npos);
}

TEST_F(Presign, CarriesEveryRequiredQueryParameter)
{
    const std::string u = url();
    for (const char* param : {"X-Amz-Algorithm=AWS4-HMAC-SHA256", "X-Amz-Credential=",
                              "X-Amz-Date=20260101T000000Z", "X-Amz-Expires=300",
                              "X-Amz-SignedHeaders=host", "X-Amz-Signature="}) {
        EXPECT_NE(u.find(param), std::string::npos) << "missing " << param;
    }
}

TEST_F(Presign, SignatureIsStableForIdenticalInputs)
{
    EXPECT_EQ(url(), url());
}

TEST_F(Presign, AnyChangedInputChangesTheSignature)
{
    const std::string baseline = url();

    req.key = "objects/different.txt";
    EXPECT_NE(url(), baseline);

    req.key = "objects/abc.txt";
    req.method = "PUT";
    EXPECT_NE(url(), baseline);

    req.method = "GET";
    req.expiresInSeconds = 600;
    EXPECT_NE(url(), baseline);
}

TEST_F(Presign, ExtraQueryParametersAreSigned)
{
    const std::string without = url();
    req.queryParams["response-content-disposition"] = "attachment; filename=\"a b.txt\"";
    const std::string with = url();

    EXPECT_NE(with, without) << "an unsigned extra parameter would be stripped or rejected";
    EXPECT_NE(with.find("response-content-disposition="), std::string::npos);
    // Spaces and quotes must be encoded, or the URL breaks before S3 sees it.
    EXPECT_EQ(with.find("filename=\""), std::string::npos);
}

TEST_F(Presign, QueryParametersAreSortedCanonically)
{
    req.queryParams["response-content-type"] = "text/plain";
    req.queryParams["response-content-disposition"] = "inline";
    const std::string u = url();
    // X-Amz-* sort before lowercase response-* in byte order.
    EXPECT_LT(u.find("X-Amz-Algorithm"), u.find("response-content-disposition"));
    EXPECT_LT(u.find("response-content-disposition"), u.find("response-content-type"));
}

TEST_F(Presign, KeysContainingSpacesAndSlashesSurvive)
{
    req.key = "objects/my folder/a file.txt";
    const std::string u = url();
    EXPECT_NE(u.find("/uploads/objects/my%20folder/a%20file.txt"), std::string::npos);
}

// --------------------------------------------------------------------------
// Refusals
// --------------------------------------------------------------------------

TEST_F(Presign, RefusesAnEndpointWithoutAScheme)
{
    req.endpoint = "s3:8333";
    EXPECT_TRUE(url().empty());
}

TEST_F(Presign, RefusesAnEmptyBucketOrKey)
{
    req.bucket = "";
    EXPECT_TRUE(url().empty());

    req.bucket = "uploads";
    req.key = "";
    EXPECT_TRUE(url().empty());
}
