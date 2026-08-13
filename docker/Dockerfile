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

# Dependency files first, so a source-only change does not rebuild Drogon.
COPY conanfile.txt CMakeLists.txt ./
RUN conan install . --build=missing -s compiler.cppstd=17 -of=build

COPY . .

RUN cmake -S . -B build \
    -DCMAKE_TOOLCHAIN_FILE=/app/build/conan_toolchain.cmake \
    -DCMAKE_POLICY_DEFAULT_CMP0091=NEW \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_POSTGRESQL=ON \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    && cmake --build build -j$(nproc)

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
