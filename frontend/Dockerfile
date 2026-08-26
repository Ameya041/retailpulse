# Frontend: build with Node, serve with nginx.
#
# The runtime image contains no Node, no npm and no source -- only static
# files and a web server. That is the whole point of a multi-stage build here:
# a React app needs a large toolchain to *build* and nothing at all to *serve*.

# ---------------------------------------------------------------------------
# Stage 1: build
# ---------------------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /build

# package.json and the lockfile first, so `npm ci` is cached and only re-runs
# when dependencies actually change rather than on every source edit.
COPY frontend/package.json frontend/package-lock.json* ./

# `npm ci` rather than `npm install`: it installs exactly what the lockfile
# says and fails if the lockfile is out of date, so a build is reproducible.
RUN npm ci --no-audit --no-fund

COPY frontend/ ./

RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: serve
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

COPY infra/nginx/frontend.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q --spider http://localhost/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
