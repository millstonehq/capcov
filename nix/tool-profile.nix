# One tool family as a single store path: bin/ with its commands and
# etc/capcov-env.sh with its variables. A container image installs it as a
# profile, so the image holds that family's run-time closure and nothing else.
#
#   nix build --impure --expr 'import ./nix/tool-profile.nix { family = "souffle"; }'
{ family, system ? builtins.currentSystem }:

let
  pkgs = import ./pinned.nix { inherit system; };
  tools = import (./tools + "/${family}") { inherit pkgs; };
  exports = pkgs.lib.concatStrings (pkgs.lib.mapAttrsToList
    (name: value: "export ${name}=${pkgs.lib.escapeShellArg (toString value)}\n")
    tools.env);
in
pkgs.buildEnv {
  name = "capcov-tools-${family}";
  paths = tools.packages ++ [ (pkgs.writeTextDir "etc/capcov-env.sh" exports) ];
  pathsToLink = [ "/bin" "/etc" ];
}
