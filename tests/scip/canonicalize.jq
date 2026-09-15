# Canonicalize `scip print --json` output so an index built in one sandbox can be
# compared byte-for-byte with a committed golden. Run with `jq -S -f` so keys are
# sorted as well. See tests/scip/README.md for the rationale of each step.
#
# 1. metadata.project_root is an absolute file:// URI of the build directory and
#    is the only host-specific field in the index; drop it.
# 2. Documents are emitted in package-walk order; sort by relative_path.
# 3. Within a document, occurrences are sorted by (range, symbol) and symbol
#    information by symbol; scip-go's emission order is not part of the contract.
# 4. external_symbols may be absent or unordered; normalize to a list sorted by symbol.
del(.metadata.project_root)
| .documents |= (
    (. // [])
    | map(
        (.occurrences |= ((. // []) | sort_by(.range, .symbol)))
        | (.symbols |= ((. // []) | sort_by(.symbol)))
      )
    | sort_by(.relative_path)
  )
| .external_symbols |= ((. // []) | sort_by(.symbol))
