# What every test and check script starts: Python, uv, git and the shell
# utilities the scripts call. No external analysis tool.
{ pkgs }:

rec {
  python = pkgs.python3;
  packages = with pkgs; [
    python
    uv
    gitMinimal
    jq
    bash
    coreutils
    diffutils
    findutils
    gawk
    gnugrep
    gnupatch
    gnused
    gnutar
    gzip
  ];
  env = {
    UV_PYTHON = "${python}/bin/python3";
    UV_PYTHON_DOWNLOADS = "never";
    UV_PYTHON_PREFERENCE = "only-system";
  };
}
