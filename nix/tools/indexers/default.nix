# The SCIP indexers and the languages they index: Go with scip-go, PHP with
# scip-php, scip-python on Node, and the scip CLI that reads every index. One
# family: the tests that need them run for seconds, and each needs scip.
{ pkgs }:

rec {
  go = pkgs.go_1_27;
  scipPython = pkgs.callPackage ./scip-python.nix { };
  scipPhp = pkgs.callPackage ./scip-php.nix { };
  packages = [ go pkgs.scip pkgs.scip-go scipPython pkgs.php ];
  env = {
    GOTOOLCHAIN = "local";
    # This family carries no C compiler, and `go run` of a program that imports
    # net would otherwise ask cgo for one.
    CGO_ENABLED = "0";
    # capcov runs scip-php as `php <script>`.
    SCIP_PHP_BIN = scipPhp.script;
  };
}
