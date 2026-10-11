# Souffle. Its closure carries the C++ compiler: souffle-compile.py names it,
# and the compiled-checker tests build binaries with it at run time.
{ pkgs }:

{
  packages = [ pkgs.souffle ];
  env = { };
}
