#!/bin/sh
set -eu

version=${1:?git version}
prefix=${2:?install prefix}

case $version in
  2.23.0|2.39.3) ;;
  *)
    echo "unsupported git version: $version" >&2
    exit 1
    ;;
esac

if [ -x "$prefix/bin/git" ]; then
  "$prefix/bin/git" --version
  exit 0
fi

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
curl -fsSL -o "$workdir/git.tar.gz" \
  "https://mirrors.edge.kernel.org/pub/software/scm/git/git-${version}.tar.gz"
tar -xzf "$workdir/git.tar.gz" -C "$workdir"
set -- \
  "prefix=$prefix" \
  NO_GETTEXT=1 \
  NO_TCLTK=1 \
  NO_OPENSSL=1 \
  NO_CURL=1 \
  NO_PERL=1 \
  NO_PYTHON=1
make -C "$workdir/git-${version}" -j"$(nproc)" "$@"
make -C "$workdir/git-${version}" install "$@"
"$prefix/bin/git" --version
