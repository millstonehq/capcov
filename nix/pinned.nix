# The nixpkgs revision flake.lock pins, for code that is not evaluated as a flake.
# The flake's own `nixpkgs.legacyPackages.<system>` is the same package set.
{ system ? builtins.currentSystem }:

let
  lock = builtins.fromJSON (builtins.readFile ../flake.lock);
in
import (builtins.fetchTree lock.nodes.nixpkgs.locked) { inherit system; }
