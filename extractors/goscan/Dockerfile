# Go AST analyzer. Emits (file, module, symbol) refs and exported defs as JSON.
# Used by both the oracle and the enrich pass for Go corpora - the Go equivalent
# of what Python's stdlib `ast` gives us for free.
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY main.go .
RUN go mod init goscan >/dev/null 2>&1 && \
    CGO_ENABLED=0 go build -o /goscan main.go

FROM alpine:3
COPY --from=build /goscan /usr/local/bin/goscan
ENTRYPOINT ["/usr/local/bin/goscan"]
