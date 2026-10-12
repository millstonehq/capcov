VERSION 0.8
PROJECT millstonehq/capcov

# NOTE: SRC_PATH defaults to "." for the standalone repository.
# A monorepo can delegate with --SRC_PATH=<directory> to find the files.
#
# Two environments. +deps, +lint, +test and +build are the plain Python install.
# The targets from +nix-base on run the whole suite and the check scripts on the
# pinned tools of nix/tools, one container image per tool family:
#
#   base      Python, uv, git and the locked Python environment
#   souffle   base and Souffle with its C++ compiler
#   shen      base and the Shen runtime with bifrost
#   indexers  base and Go, PHP, scip, scip-go, scip-php, scip-python
#
# tests/tool-families.json says which test classes need which family. A shard of
# the suite runs in the image of its family and nowhere else.
#
# BASE_IMAGE, SOUFFLE_IMAGE, SHEN_IMAGE and INDEXERS_IMAGE name published copies
# of those images. Empty, the default, an image is built here. Set, the targets
# of that family start from it and build nothing. +tool-image-push pushes one.
# The caller chooses the names. A tag should be derived from flake.lock,
# nix/pinned.nix, nix/tool-profile.nix, nix/build-profile.sh, nix/with-toolchain,
# nix/tools/base, nix/tools/<family>, pyproject.toml and uv.lock.

# deps installs the locked dependencies, with the three extras and the lint group
deps:
    ARG SRC_PATH=.
    FROM ghcr.io/millstonehq/python:3.14
    WORKDIR /app

    COPY ${SRC_PATH}/pyproject.toml ${SRC_PATH}/uv.lock ./
    RUN uv sync --frozen --no-install-project --extra probe-python --extra treesitter --extra faults-property --group lint

# src adds the source tree and installs the project itself
src:
    ARG SRC_PATH=.
    FROM +deps --SRC_PATH=${SRC_PATH}

    COPY ${SRC_PATH}/README.md ${SRC_PATH}/LICENSE ${SRC_PATH}/MANIFEST.in \
         ${SRC_PATH}/capcov.toml ${SRC_PATH}/capcov.outcomes.json ./
    COPY --dir ${SRC_PATH}/src ${SRC_PATH}/tests ${SRC_PATH}/scripts ${SRC_PATH}/benchmarks \
         ${SRC_PATH}/examples ${SRC_PATH}/experiments ${SRC_PATH}/shen ./
    RUN uv sync --frozen --extra probe-python --extra treesitter --extra faults-property --group lint

# lint runs ruff with the rules selected in pyproject.toml
lint:
    ARG SRC_PATH=.
    FROM +src --SRC_PATH=${SRC_PATH}

    RUN uv run --frozen --group lint ruff check .

# test runs the engine suite (tests/test_*.py) with the probe-python, treesitter and
# faults-property extras. This image has no go, souffle, scip or git, so the tests that need them
# skip here and run in +test-pinned. check-skips.py fails the target when the
# skipped tests are not exactly the ones tests/skips-allowed-plain-python.json
# lists. tests/claim_semantics needs souffle throughout and is left to +test-pinned.
test:
    ARG SRC_PATH=.
    FROM +src --SRC_PATH=${SRC_PATH}

    RUN PYTHONPATH=src uv run --frozen --extra probe-python --extra treesitter --extra faults-property \
        python scripts/check-skips.py --allowlist tests/skips-allowed-plain-python.json \
        $(ls tests/test_*.py | sed 's|/|.|; s|\.py$||')

# build makes the wheel and sdist, installs the wheel into a clean environment
# and checks that both console scripts start and the version matches
build:
    ARG SRC_PATH=.
    FROM +src --SRC_PATH=${SRC_PATH}

    RUN uv build
    RUN sh scripts/check-wheel.sh dist

    SAVE ARTIFACT dist AS LOCAL dist

# nix-base is the layer every family image shares: Nix, and the base family
# installed as a profile. Only flake.lock and the files of the base family are
# copied, so the layer is rebuilt when one of those changes.
nix-base:
    ARG SRC_PATH=.
    ARG NIX_IMAGE=nixos/nix:2.31.1
    FROM ${NIX_IMAGE}
    RUN echo 'experimental-features = nix-command flakes' >> /etc/nix/nix.conf
    WORKDIR /toolchain
    COPY ${SRC_PATH}/flake.lock ./
    COPY ${SRC_PATH}/nix/pinned.nix ${SRC_PATH}/nix/tool-profile.nix ${SRC_PATH}/nix/build-profile.sh \
         ${SRC_PATH}/nix/with-toolchain nix/
    COPY --dir ${SRC_PATH}/nix/tools/base nix/tools/
    RUN sh nix/build-profile.sh base \
     && cp nix/with-toolchain /toolchain/with-toolchain \
     && /toolchain/with-toolchain sh -c 'python3 --version && uv --version && git --version && jq --version && patch --version | head -1'

# tool-layer adds one family's profile on top of +nix-base. The build and the
# garbage collection are one RUN, so the layer holds the family's run-time
# closure and not what building it fetched.
tool-layer:
    ARG SRC_PATH=.
    ARG --required FAMILY
    FROM +nix-base --SRC_PATH=${SRC_PATH}
    COPY --dir ${SRC_PATH}/nix/tools/${FAMILY} nix/tools/
    RUN sh nix/build-profile.sh ${FAMILY} && ls /nix/var/nix/profiles/capcov-${FAMILY}/bin

# venv installs the locked Python environment on the base family's Python, with
# the extras the suite uses and the pytest the outcome check starts.
venv:
    ARG SRC_PATH=.
    FROM +nix-base --SRC_PATH=${SRC_PATH}
    WORKDIR /app
    COPY ${SRC_PATH}/pyproject.toml ${SRC_PATH}/uv.lock ./
    RUN /toolchain/with-toolchain uv sync --frozen --no-install-project \
            --extra probe-python --extra treesitter --extra faults-property --group packaging --group lint \
     && /toolchain/with-toolchain uv run --frozen --no-sync --with pytest python -m pytest --version
    SAVE ARTIFACT /app app
    SAVE ARTIFACT /root/.cache/uv uv-cache

# tool-image is the image of one family: its Nix layers, then the Python
# environment. The environment is copied in last, so a change to uv.lock does
# not rebuild a tool.
tool-image:
    ARG SRC_PATH=.
    ARG --required FAMILY
    FROM +nix-base --SRC_PATH=${SRC_PATH}
    IF [ "${FAMILY}" != "base" ]
        FROM +tool-layer --SRC_PATH=${SRC_PATH} --FAMILY=${FAMILY}
    END
    COPY (+venv/app --SRC_PATH=${SRC_PATH}) /app
    COPY (+venv/uv-cache --SRC_PATH=${SRC_PATH}) /root/.cache/uv
    WORKDIR /app

# tool-image-push pushes +tool-image of FAMILY as IMAGE (with `earth --push`)
tool-image-push:
    ARG SRC_PATH=.
    ARG --required FAMILY
    ARG --required IMAGE
    FROM +tool-image --SRC_PATH=${SRC_PATH} --FAMILY=${FAMILY}
    SAVE IMAGE --push ${IMAGE}

# env-pinned is the environment of one family: the published IMAGE when one is
# named, +tool-image built here otherwise.
env-pinned:
    ARG SRC_PATH=.
    ARG FAMILY=base
    ARG IMAGE=
    ARG NIX_IMAGE=nixos/nix:2.31.1
    FROM ${NIX_IMAGE}
    IF [ -n "${IMAGE}" ]
        FROM ${IMAGE}
    ELSE
        FROM +tool-image --SRC_PATH=${SRC_PATH} --FAMILY=${FAMILY}
    END

# src-pinned adds the source tree to the environment of one family
src-pinned:
    ARG SRC_PATH=.
    ARG FAMILY=base
    ARG IMAGE=
    FROM +env-pinned --SRC_PATH=${SRC_PATH} --FAMILY=${FAMILY} --IMAGE=${IMAGE}
    WORKDIR /app
    COPY ${SRC_PATH}/README.md ${SRC_PATH}/LICENSE ${SRC_PATH}/MANIFEST.in \
         ${SRC_PATH}/capcov.toml ${SRC_PATH}/capcov.outcomes.json \
         ${SRC_PATH}/flake.nix ${SRC_PATH}/flake.lock ${SRC_PATH}/package.nix ./
    COPY --dir ${SRC_PATH}/src ${SRC_PATH}/tests ${SRC_PATH}/scripts ${SRC_PATH}/benchmarks \
         ${SRC_PATH}/examples ${SRC_PATH}/experiments ${SRC_PATH}/shen ${SRC_PATH}/nix ./
    RUN /toolchain/with-toolchain uv sync --frozen \
            --extra probe-python --extra treesitter --extra faults-property --group packaging --group lint

# compiled-checkers compiles the Souffle program of each rule pack into a cache
# mount that the souffle shards share, both packs at once. An entry is named by
# the hash of the program, the souffle executable, its compiler configuration and
# the flags, and its binary is hashed again before use, so a changed rule pack or
# a changed souffle is a miss and compiles again. With the entries present the
# RUN takes seconds. With the mount emptied, the tests compile what they need.
compiled-checkers:
    ARG SRC_PATH=.
    ARG IMAGE=
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=souffle --IMAGE=${IMAGE}
    RUN --mount=type=cache,id=capcov-souffle-compiled,sharing=shared,target=/var/cache/capcov-souffle \
        for pack in replay static; do \
            PYTHONPATH=src /toolchain/with-toolchain uv run --frozen --no-sync python scripts/compiled_checker.py \
                compile --pack "$pack" --cache-dir /var/cache/capcov-souffle > "$pack.json" & \
        done; \
        wait; \
        /toolchain/with-toolchain jq -r '"\(.pack) \(.compile_key) cache_hit=\(.cache_hit) compile_seconds=\(.compile_seconds)"' replay.json static.json \
     && /toolchain/with-toolchain jq -r .compile_key replay.json static.json > compile-keys.txt
    SAVE ARTIFACT compile-keys.txt

# test-shard runs shard SHARD of SHARDS of one family's test classes, in that
# family's image, and saves what it ran and what skipped. A failing test fails
# the target. The skip set is not judged here: +test-pinned judges the union.
test-shard:
    ARG SRC_PATH=.
    ARG --required FAMILY
    ARG IMAGE=
    ARG SHARDS=1
    ARG SHARD=1
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=${FAMILY} --IMAGE=${IMAGE}
    IF [ "${FAMILY}" = "souffle" ]
        COPY (+compiled-checkers/compile-keys.txt --SRC_PATH=${SRC_PATH} --IMAGE=${IMAGE}) /tmp/
    END
    RUN --mount=type=cache,id=capcov-souffle-compiled,sharing=shared,target=/var/cache/capcov-souffle \
        CAPCOV_SOUFFLE_CACHE_DIR=/var/cache/capcov-souffle PYTHONPATH=src \
        /toolchain/with-toolchain uv run --frozen --no-sync \
        python scripts/check-skips.py --family ${FAMILY} --shard ${SHARD}/${SHARDS} --result shard.json
    SAVE ARTIFACT shard.json

# test-pinned is the one full run of the suite: every family in its own image,
# the souffle and shen families in SOUFFLE_SHARDS and SHEN_SHARDS parts, all at
# once, which keeps the job inside its twelve-minute limit. Shen's only latency
# assertion allows 15s for a 2s timeout, so the shared load does not threaten it. The gate over their union in the base image discovers the suite again and fails
# unless the shards ran every discovered test exactly once, and unless the tests
# that skipped are exactly the ones tests/skips-allowed.json lists. It saves the
# timings of the run in the form of tests/shard-timings.json.
test-pinned:
    ARG SRC_PATH=.
    ARG BASE_IMAGE=
    ARG SOUFFLE_IMAGE=
    ARG SHEN_IMAGE=
    ARG INDEXERS_IMAGE=
    ARG SOUFFLE_SHARDS=3
    ARG SHEN_SHARDS=2
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE}
    WAIT
        BUILD +test-shard --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE}
        BUILD +test-shard --SRC_PATH=${SRC_PATH} --FAMILY=indexers --IMAGE=${INDEXERS_IMAGE}
        FOR shard IN $(seq 1 ${SOUFFLE_SHARDS})
            BUILD +test-shard --SRC_PATH=${SRC_PATH} --FAMILY=souffle --IMAGE=${SOUFFLE_IMAGE} --SHARDS=${SOUFFLE_SHARDS} --SHARD=${shard}
        END
        FOR shard IN $(seq 1 ${SHEN_SHARDS})
            BUILD +test-shard --SRC_PATH=${SRC_PATH} --FAMILY=shen --IMAGE=${SHEN_IMAGE} --SHARDS=${SHEN_SHARDS} --SHARD=${shard}
        END
    END
    COPY (+test-shard/shard.json --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE}) shards/base.json
    COPY (+test-shard/shard.json --SRC_PATH=${SRC_PATH} --FAMILY=indexers --IMAGE=${INDEXERS_IMAGE}) shards/indexers.json
    FOR shard IN $(seq 1 ${SOUFFLE_SHARDS})
        COPY (+test-shard/shard.json --SRC_PATH=${SRC_PATH} --FAMILY=souffle --IMAGE=${SOUFFLE_IMAGE} --SHARDS=${SOUFFLE_SHARDS} --SHARD=${shard}) \
             shards/souffle-${shard}.json
    END
    FOR shard IN $(seq 1 ${SHEN_SHARDS})
        COPY (+test-shard/shard.json --SRC_PATH=${SRC_PATH} --FAMILY=shen --IMAGE=${SHEN_IMAGE} --SHARDS=${SHEN_SHARDS} --SHARD=${shard}) \
             shards/shen-${shard}.json
    END
    RUN PYTHONPATH=src /toolchain/with-toolchain uv run --frozen --no-sync \
        python scripts/check-skips.py --merge shards/*.json --write-timings shard-timings.json
    SAVE ARTIFACT shard-timings.json

# check-backpressure runs capcov's own outcome check against three copies of the
# source: the baseline must advance and the two faulted copies must be blocked.
check-backpressure:
    ARG SRC_PATH=.
    ARG BASE_IMAGE=
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE}
    RUN /toolchain/with-toolchain uv run --frozen --no-sync --with pytest sh scripts/check-backpressure.sh

# check-real-go-flow gates a real Go service and rejects its lost-write mutant
check-real-go-flow:
    ARG SRC_PATH=.
    ARG INDEXERS_IMAGE=
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=indexers --IMAGE=${INDEXERS_IMAGE}
    RUN /toolchain/with-toolchain uv run --frozen --no-sync sh scripts/check-real-go-flow.sh

# check-fault-harness runs the fault-notes example under every crash point, fault
# and bounded schedule, and requires its two patched copies to be rejected
check-fault-harness:
    ARG SRC_PATH=.
    ARG BASE_IMAGE=
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE}
    RUN /toolchain/with-toolchain uv run --frozen --no-sync sh scripts/check-fault-harness.sh

# flake-check builds CHECKS, attributes of the flake's checks, in the image of
# FAMILY, where that family's tools are already valid store paths. With no
# CHECKS it evaluates every output of the flake and builds nothing.
flake-check:
    ARG SRC_PATH=.
    ARG FAMILY=base
    ARG IMAGE=
    ARG CHECKS=
    FROM +src-pinned --SRC_PATH=${SRC_PATH} --FAMILY=${FAMILY} --IMAGE=${IMAGE}
    IF [ -z "${CHECKS}" ]
        RUN nix flake check --no-update-lock-file --no-build .
    ELSE
        RUN system=$(nix eval --impure --raw --expr builtins.currentSystem) \
         && for check in ${CHECKS}; do \
                nix build --no-update-lock-file --no-link --print-out-paths ".#checks.$system.$check" || exit 1; \
            done
    END

# flake-checks is what `nix flake check` does, split by the image that holds the
# tools: the evaluation and the package in base, each smoke with its tool.
flake-checks:
    ARG SRC_PATH=.
    ARG BASE_IMAGE=
    ARG SOUFFLE_IMAGE=
    ARG SHEN_IMAGE=
    ARG INDEXERS_IMAGE=
    BUILD +flake-check --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE}
    BUILD +flake-check --SRC_PATH=${SRC_PATH} --FAMILY=base --IMAGE=${BASE_IMAGE} --CHECKS=capcov-package
    BUILD +flake-check --SRC_PATH=${SRC_PATH} --FAMILY=souffle --IMAGE=${SOUFFLE_IMAGE} --CHECKS=souffle-recursive-typed-smoke
    BUILD +flake-check --SRC_PATH=${SRC_PATH} --FAMILY=shen --IMAGE=${SHEN_IMAGE} --CHECKS="shen-package shen-evaluator-smoke"
    BUILD +flake-check --SRC_PATH=${SRC_PATH} --FAMILY=indexers --IMAGE=${INDEXERS_IMAGE} --CHECKS=scip-go-index-smoke

# all runs lint, test and build on the plain Python install
all:
    ARG SRC_PATH=.
    BUILD +lint --SRC_PATH=${SRC_PATH}
    BUILD +test --SRC_PATH=${SRC_PATH}
    BUILD +build --SRC_PATH=${SRC_PATH}

# checks runs everything except the whole suite: lint, the wheel, the three check
# scripts, the flake checks, and the engine suite on the plain Python install,
# where the tests that need a tool skip.
checks:
    ARG SRC_PATH=.
    ARG BASE_IMAGE=
    ARG SOUFFLE_IMAGE=
    ARG SHEN_IMAGE=
    ARG INDEXERS_IMAGE=
    BUILD +lint --SRC_PATH=${SRC_PATH}
    BUILD +test --SRC_PATH=${SRC_PATH}
    BUILD +build --SRC_PATH=${SRC_PATH}
    BUILD +check-backpressure --SRC_PATH=${SRC_PATH} --BASE_IMAGE=${BASE_IMAGE}
    BUILD +check-real-go-flow --SRC_PATH=${SRC_PATH} --INDEXERS_IMAGE=${INDEXERS_IMAGE}
    BUILD +check-fault-harness --SRC_PATH=${SRC_PATH} --BASE_IMAGE=${BASE_IMAGE}
    BUILD +flake-checks --SRC_PATH=${SRC_PATH} --BASE_IMAGE=${BASE_IMAGE} --SOUFFLE_IMAGE=${SOUFFLE_IMAGE} --SHEN_IMAGE=${SHEN_IMAGE} --INDEXERS_IMAGE=${INDEXERS_IMAGE}

# ci runs +checks and +test-pinned, the one run of the whole suite. BuildKit runs
# their targets, and the shards, concurrently.
ci:
    ARG SRC_PATH=.
    ARG BASE_IMAGE=
    ARG SOUFFLE_IMAGE=
    ARG SHEN_IMAGE=
    ARG INDEXERS_IMAGE=
    BUILD +checks --SRC_PATH=${SRC_PATH} --BASE_IMAGE=${BASE_IMAGE} --SOUFFLE_IMAGE=${SOUFFLE_IMAGE} --SHEN_IMAGE=${SHEN_IMAGE} --INDEXERS_IMAGE=${INDEXERS_IMAGE}
    BUILD +test-pinned --SRC_PATH=${SRC_PATH} --BASE_IMAGE=${BASE_IMAGE} --SOUFFLE_IMAGE=${SOUFFLE_IMAGE} --SHEN_IMAGE=${SHEN_IMAGE} --INDEXERS_IMAGE=${INDEXERS_IMAGE}
