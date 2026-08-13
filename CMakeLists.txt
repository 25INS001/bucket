cmake_minimum_required(VERSION 3.25)
project(bucket-service LANGUAGES CXX)

include_directories("src")

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

list(APPEND CMAKE_PREFIX_PATH "${CMAKE_BINARY_DIR}")

find_package(Drogon REQUIRED)
find_package(OpenSSL REQUIRED)
find_package(CURL REQUIRED)
find_package(GTest REQUIRED)

file(GLOB_RECURSE APP_SOURCES CONFIGURE_DEPENDS
    "src/*.cpp"
    "src/*.cc"
    "controllers/*.cc"
)

add_executable(${PROJECT_NAME} ${APP_SOURCES})

target_link_libraries(${PROJECT_NAME} PRIVATE
    Drogon::Drogon
    OpenSSL::SSL
    OpenSSL::Crypto
    CURL::libcurl
)

option(PHASICON_BUILD_TESTS "Build bucket-service unit tests" ON)

if(PHASICON_BUILD_TESTS)
    include(CTest)

    # The presigner is pure: no database, no Drogon app instance, no network.
    # That is deliberate — it is the part that has to be provably correct, so it
    # is kept testable without standing anything up.
    add_executable(bucket-service-tests
        tests/unit/test_s3_presigner.cpp
        src/services/S3Presigner.cpp
    )

    target_include_directories(bucket-service-tests PRIVATE src)
    target_link_libraries(bucket-service-tests PRIVATE
        GTest::gtest_main
        OpenSSL::SSL
        OpenSSL::Crypto
    )

    include(GoogleTest)
    gtest_discover_tests(bucket-service-tests)
endif()
