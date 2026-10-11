# bifrost, the launcher the Shen runtime tests drive: `bifrost run --impl shen-go`.
# It starts the shen-go binary named by BIFROST_SHEN_GO; it is not a Shen runtime.
{
  lib,
  buildGoModule,
  fetchFromGitHub,
}:

buildGoModule {
  pname = "bifrost";
  version = "0-unstable-2026-09-27";

  src = fetchFromGitHub {
    owner = "pyrex41";
    repo = "bifrost";
    rev = "65236412511ace29934163cc21a418de4223b234";
    hash = "sha256-0jgu3HQeUWcbLDGgyqp0PF2Sj93smf1MwIdIarzpuUY=";
  };

  # The module has no dependencies outside the standard library.
  vendorHash = null;

  # Its own tests look for sibling checkouts of every Shen port.
  doCheck = false;

  meta = {
    description = "One command line for every Shen port";
    homepage = "https://github.com/pyrex41/bifrost";
    license = lib.licenses.asl20;
    mainProgram = "bifrost";
  };
}
