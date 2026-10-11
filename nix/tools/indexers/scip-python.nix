# scip-python, the SCIP indexer for Python, from the published npm package.
# The package ships a built bundle and no lock file, so the lock beside this file
# pins its runtime dependencies. To regenerate it for a new version: unpack the
# tarball, delete devDependencies and scripts from package.json, and run
# `npm install --package-lock-only --ignore-scripts`.
{
  lib,
  buildNpmPackage,
  fetchurl,
  jq,
}:

buildNpmPackage rec {
  pname = "scip-python";
  version = "0.6.6";

  src = fetchurl {
    url = "https://registry.npmjs.org/@sourcegraph/scip-python/-/scip-python-${version}.tgz";
    hash = "sha256-vo4KHsGAQjxg6fLCZywgj0mjWraQrA+PElnkIj29bkI=";
  };
  sourceRoot = "package";

  postPatch = ''
    ${lib.getExe jq} 'del(.devDependencies) | del(.scripts)' package.json > package.json.runtime
    mv package.json.runtime package.json
    cp ${./scip-python-package-lock.json} package-lock.json
  '';

  npmDepsHash = "sha256-hiDAEChUeHtUzd4FhFhm3cI9Qsjq4Y5G2mhDCIuIrVg=";
  dontNpmBuild = true;

  meta = {
    description = "SCIP indexer for Python";
    homepage = "https://github.com/sourcegraph/scip-python";
    license = lib.licenses.mit;
    mainProgram = "scip-python";
  };
}
