# ----------- Build Stage -----------
FROM debian:bookworm-slim AS dev

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential cmake pkg-config curl python3-venv python3-pip git make \
    libpq-dev uuid-dev postgresql-client openssl libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/conan-venv \
    && /opt/conan-venv/bin/pip install --no-cache-dir conan
ENV PATH="/opt/conan-venv/bin:$PATH"
RUN conan profile detect --force

# Build parallelism from memory, not core count.
#
# -j$(nproc) put four concurrent C++ compilers on Drogon headers into node2's
# 4 GiB. The OOM killer took the build and the runner agent with it, so the job
# came back from GitHub as a failure with no failing step and no log — the
# signature of a runner that stopped answering rather than a compiler that
# returned non-zero. Every C++ service failed on arm64; the Python ones passed.
#
# Roughly 2 GiB per compiler, capped at 4 jobs and at the core count, never
# fewer than one. That is -j2 on the 4 GiB Pi and -j4 on the 12-core amd64 box.
#
# The ceiling is deliberate rather than derived. navrobotec is a workstation
# that also runs cluster workloads, so handing a build all 12 cores competes
# with everything else on it. A slower build that finishes beats a fast one
# that gets killed.
RUN CEILING=4; \
    JOBS=$(awk '/MemTotal/{j=int($2/2097152); if (j<1) j=1; print j}' /proc/meminfo); \
    CORES=$(nproc); \
    if [ "$JOBS" -gt "$CORES" ]; then JOBS="$CORES"; fi; \
    if [ "$JOBS" -gt "$CEILING" ]; then JOBS="$CEILING"; fi; \
    echo "$JOBS" > /build-jobs; \
    echo "build parallelism: -j$JOBS (cores=$CORES, ceiling=$CEILING)"

# Dependency files first, so a source-only change does not rebuild Drogon.
COPY conanfile.txt CMakeLists.txt ./
# Conan compiles missing dependencies from source, so it needs the same cap:
# building Drogon itself is heavier than building this service.
RUN conan install . --build=missing -s compiler.cppstd=17 -of=build \
    -c tools.build:jobs=$(cat /build-jobs)

COPY . .

RUN cmake -S . -B build \
    -DCMAKE_TOOLCHAIN_FILE=/app/build/conan_toolchain.cmake \
    -DCMAKE_POLICY_DEFAULT_CMP0091=NEW \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_POSTGRESQL=ON \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    && cmake --build build -j$(cat /build-jobs)

# ----------- Runtime Stage -----------
FROM debian:bookworm-slim AS runtime

WORKDIR /app
RUN mkdir -p /app/src

# The binary and the config TEMPLATE only. The rendered config carries the
# database password and the S3 secret key, so it is produced at runtime by the
# entrypoint rather than baked into a layer anyone who can pull the image reads.
COPY --from=dev /app/build/bucket-service /app/bucket-service
COPY --from=dev /app/src/config.json.template /app/src/config.json.template
COPY docker/entrypoint.sh /app/entrypoint.sh

RUN apt-get update && apt-get install -y ca-certificates gettext-base libpq5 \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && chmod +x /app/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
