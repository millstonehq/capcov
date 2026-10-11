{
  description = "capcov: capability coverage, with the pinned toolchain its checks run on";

  inputs.nixpkgs.url =
    "github:NixOS/nixpkgs/34ab99075ac4f7e40cf037eef32cb1c360bb85e9";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "aarch64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      eachSystem = nixpkgs.lib.genAttrs systems;
      # One attribute per tool family, defined under nix/tools. The dev shells and
      # the container images of the Earthfile are built from the same definitions.
      familyNames = [ "base" "souffle" "shen" "indexers" ];
      forSystem = system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          families = nixpkgs.lib.genAttrs familyNames
            (name: import (./nix/tools + "/${name}") { inherit pkgs; });
          capcov = pkgs.callPackage ./package.nix { };
        in
        {
          inherit pkgs families capcov;
          go = families.indexers.go;
          python = families.base.python;
          shenGo = families.shen.shenGo;
          bifrost = families.shen.bifrost;
          scipPython = families.indexers.scipPython;
          scipPhp = families.indexers.scipPhp;
        };
    in
    {
      packages = eachSystem (system:
        let values = forSystem system;
        in {
          capcov = values.capcov;
          capcov-treesitter = values.capcov.override { withTreesitter = true; };
          shen-go = values.shenGo;
          bifrost = values.bifrost;
          scip-python = values.scipPython;
          scip-php = values.scipPhp;
          default = values.capcov;
        });

      apps = eachSystem (system: {
        default = {
          type = "app";
          program = "${(forSystem system).capcov}/bin/capcov";
          meta.description = "Run the capcov command";
        };
      });

      devShells = eachSystem (system:
        let
          values = forSystem system;
          lib = values.pkgs.lib;
          # A shell with the base family and the named ones.
          shellOf = names: extra:
            let
              chosen = map (name: values.families.${name}) (lib.unique ([ "base" ] ++ names));
              packages = extra ++ lib.concatMap (family: family.packages) chosen;
            in
            { inherit packages; environment = lib.foldl' (acc: family: acc // family.env) { } chosen; };
          # Interactive use only; no test starts these.
          interactive = with values.pkgs; [ git hyperfine ];
          everything = shellOf familyNames interactive;
          # macOS login shells run path_helper after `nix develop` sets PATH.
          # The workflow invokes `bash -lc`, so interpose a transparent Nix bash
          # that restores the pinned tool prefix through BASH_ENV.
          bashEnvOf = packages: values.pkgs.writeText "capcov-bash-env" ''
            export PATH="${lib.makeBinPath packages}:$PATH"
          '';
          workflowBashOf = packages: values.pkgs.writeShellScriptBin "bash" ''
            export BASH_ENV=${bashEnvOf packages}
            exec ${values.pkgs.bashInteractive}/bin/bash --noprofile "$@"
          '';
          mkShellOf = shell:
            let packages = [ (workflowBashOf shell.packages) ] ++ shell.packages;
            in values.pkgs.mkShell (shell.environment // {
              inherit packages;
              BASH_ENV = bashEnvOf shell.packages;
              shellHook = ''
                export PATH="${lib.makeBinPath packages}:$PATH"
                python -c 'import sys; assert sys.version_info[:2] >= (3, 12), sys.version'
              '';
            });
        in
        # `default` has every family and the interactive tools. `base`, `souffle`,
        # `shen` and `indexers` have the base family and that one, for a job that
        # runs only the tests of one family.
        { default = mkShellOf everything; }
        // lib.genAttrs familyNames (name: mkShellOf (shellOf [ name ] [ ])));

      checks = eachSystem (system:
        let
          values = forSystem system;
          pkgs = values.pkgs;
          goAppSource = pkgs.lib.cleanSource ./tests/fixtures/go_app;
          goAppGolden = ./tests/fixtures/scip_go_app_index.json;
        in {
          capcov-package = values.capcov;

          shen-package = values.shenGo;

          shen-evaluator-smoke = pkgs.runCommand "shen-evaluator-smoke" {
            nativeBuildInputs = [ values.shenGo ];
          } ''
            mkdir -p "$out"
            shen --version | tee "$out/version.txt"
            shen eval -e '(+ 20 22)' | tee "$out/evaluation.txt"
            grep -Fx '42' "$out/evaluation.txt"
            if shen eval -e '(+ 1' >"$out/malformed.stdout" 2>"$out/malformed.stderr"; then
              echo "malformed Shen unexpectedly succeeded" >&2
              exit 1
            fi
          '';

          souffle-recursive-typed-smoke = pkgs.runCommand "souffle-recursive-typed-smoke" {
            nativeBuildInputs = [ pkgs.souffle ];
          } ''
            export LC_ALL=C
            mkdir -p facts output "$out"
            cp ${./tests/souffle/recursive-typed.dl} recursive-typed.dl
            cp ${./tests/souffle/edge.facts} facts/edge.facts
            souffle --version | tee "$out/version.txt"
            souffle -F facts -D output recursive-typed.dl
            sort output/reachable.csv > reachable.sorted.csv
            diff -u ${./tests/souffle/reachable.expected.csv} reachable.sorted.csv
            cp reachable.sorted.csv "$out/reachable.csv"
          '';

          # Index the Go fixture with the pinned scip-go, print it with the pinned
          # scip CLI, canonicalize, and compare byte-for-byte with the committed golden.
          # go.mod has no `require` lines, so no module download is needed.
          scip-go-index-smoke = pkgs.runCommand "scip-go-index-smoke" {
            nativeBuildInputs = [ values.go pkgs.scip pkgs.scip-go pkgs.jq ];
          } ''
            export HOME="$TMPDIR/home" GOCACHE="$TMPDIR/gocache" GOPATH="$TMPDIR/gopath"
            export GOFLAGS=-mod=mod GOPROXY=off GOTOOLCHAIN=local LC_ALL=C
            mkdir -p "$HOME" "$GOCACHE" "$GOPATH" "$out"
            cp -R ${goAppSource} go_app
            chmod -R u+w go_app
            ( cd go_app && scip-go --output index.scip )
            scip print --json go_app/index.scip > raw.json
            jq -S -f ${./tests/scip/canonicalize.jq} raw.json > canonical.json
            diff -u ${goAppGolden} canonical.json
            cp canonical.json "$out/scip_go_app_index.json"
            scip --version | tee "$out/scip-version.txt"
            { scip-go --version 2>&1 || true; } | tee "$out/scip-go-version.txt"
            go version | tee "$out/go-version.txt"
            sha256sum go_app/index.scip | tee "$out/index.scip.sha256"
          '';
        });
    };
}
