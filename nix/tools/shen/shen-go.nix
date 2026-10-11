# shen-go, the Go port of the Shen language, at a fixed commit.
{
  lib,
  buildGoModule,
  fetchFromGitHub,
  go_1_27,
  git,
}:

let
  revision = "c12933d89d7312d5d25a951bbe20d1511c3dfbea";
in
(buildGoModule.override { go = go_1_27; }) {
  pname = "shen-go";
  version = "0-unstable-2026-09-14";
  src = fetchFromGitHub {
    owner = "pyrex41";
    repo = "shen-go";
    rev = revision;
    hash = "sha256-He6LoA/M6IjuV25KK7d2DO5dW8yccloK37KRSj5jKTA=";
  };
  vendorHash = "sha256-iTtlmSlY0qbH/1waOlfRMc1qAkBacQxI6pohRPni/so=";
  subPackages = [ "cmd/shen" ];
  nativeBuildInputs = [ git ];
  env.GOTOOLCHAIN = "local";
  passthru.revision = revision;
  meta = {
    description = "Go port of the Shen language";
    homepage = "https://github.com/pyrex41/shen-go";
    license = lib.licenses.bsd3;
    mainProgram = "shen";
    platforms = [ "aarch64-darwin" "aarch64-linux" "x86_64-linux" ];
  };
}
