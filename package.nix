# The capcov command as a Nix package. The flake in this directory exposes it as
# packages.default; another flake can also call this file with its own nixpkgs:
#
#   capcov = pkgs.callPackage ./package.nix { };
#
# The core install has no runtime dependencies. `withTreesitter = true` adds the
# packages behind the `treesitter` extra, for adapters that read source through a
# tree-sitter query.
{
  lib,
  python3Packages,
  withTreesitter ? false,
}:

let
  project = (lib.importTOML ./pyproject.toml).project;
in
python3Packages.buildPythonApplication {
  pname = project.name;
  inherit (project) version;
  pyproject = true;

  # Only what the wheel is built from. Tests, fixtures and experiments stay out
  # of the source so that editing them does not rebuild the package.
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./pyproject.toml
      ./README.md
      ./LICENSE
      ./src
    ];
  };

  build-system = [ python3Packages.setuptools ];

  dependencies = lib.optionals withTreesitter [
    python3Packages.tree-sitter
    python3Packages.tree-sitter-language-pack
  ];

  # The unit suite needs git, go, souffle and a writable source tree; it runs in
  # the dev shell through scripts/check-skips.py. Here the build proves the wheel
  # imports and both console scripts start.
  doCheck = false;
  pythonImportsCheck = [ "capcov" ];
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    $out/bin/capcov --help >/dev/null
    $out/bin/capcov flows --help >/dev/null
    $out/bin/synapse-capabilities --help >/dev/null
    runHook postInstallCheck
  '';

  meta = {
    description = project.description;
    homepage = "https://github.com/millstonehq/capcov";
    license = lib.licenses.mit;
    mainProgram = "capcov";
  };
}
