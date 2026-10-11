#!/bin/sh
# Install one tool family as the profile /nix/var/nix/profiles/capcov-<family> and
# delete from the store what building it fetched. Run in the directory that holds
# flake.lock and nix/, in ONE image layer: a collection in a later layer would
# leave the build inputs in this one.
#
# Usage: nix/build-profile.sh FAMILY
set -eu

family=$1
out=$(nix build --impure --no-link --print-out-paths \
        --expr "import ./nix/tool-profile.nix { family = \"$family\"; }")
nix-env --profile "/nix/var/nix/profiles/capcov-$family" --set "$out"
rm -rf "${HOME:-/root}/.cache/nix"
nix store gc
