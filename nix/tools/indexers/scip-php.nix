# scip-php, the SCIP indexer for PHP. capcov runs it as `php <script>`, so the
# dev shell points SCIP_PHP_BIN at the script under share/php and not at a wrapper.
{
  lib,
  php,
  fetchFromGitHub,
}:

php.buildComposerProject2 (finalAttrs: {
  pname = "scip-php";
  # Composer reads this string and refuses the usual 0-unstable-<date> form.
  version = "0.0.0+unstable.2026.05.02";

  src = fetchFromGitHub {
    owner = "davidrjenni";
    repo = "scip-php";
    rev = "71a5b117ec4c5dd2af302e363410e604e5df309e";
    hash = "sha256-O0+k+UkW1mKFBv4tB8xsH6/zoOQXNnncGJDrsrJI3x4=";
  };

  vendorHash = "sha256-ecVcdkGyRAxFPJBd5vd9q59izwgiEbp8PWFmbuWnFyk=";

  passthru.script = "${finalAttrs.finalPackage}/share/php/scip-php/bin/scip-php";

  meta = {
    description = "SCIP indexer for PHP";
    homepage = "https://github.com/davidrjenni/scip-php";
    license = lib.licenses.mit;
    mainProgram = "scip-php";
  };
})
