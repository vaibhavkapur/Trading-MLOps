# syntax=docker/dockerfile:1
FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod ./
COPY src ./src
RUN CGO_ENABLED=0 GOOS=linux go build -o /usr/local/bin/drctl ./src/cmd/drctl

FROM alpine:3.19
RUN adduser -D -u 10001 druser
WORKDIR /app
COPY --from=builder /usr/local/bin/drctl /usr/local/bin/drctl
COPY config ./config
COPY data/fixtures ./data/fixtures
USER druser
ENTRYPOINT ["/usr/local/bin/drctl"]
CMD ["--help"]
