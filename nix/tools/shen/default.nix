# The Shen runtime the claim-semantics tests drive: shen-go and its launcher.
{ pkgs }:

rec {
  shenGo = pkgs.callPackage ./shen-go.nix { };
  bifrost = pkgs.callPackage ./bifrost.nix { };
  packages = [ shenGo bifrost ];
  env = {
    # bifrost starts the binary this names; the Shen tests require both.
    BIFROST_SHEN_GO = "${shenGo}/bin/shen";
  };
}
