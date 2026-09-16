\* capcov certificate output (EXPERIMENT-PLAN section 18, Stage D).

   Renders a derivation found by claim-workbench.shen as JSON in exactly the
   capcov-static-certificate-v1 shape that claims/static/certificate.recheck
   accepts unchanged.  bundle_digest and rules_digest are opaque values
   passed in from Python (capcov.*in-digests*); conclusion, derivation,
   nodes, leaves, witnesses, absent, steps, nesting, bounds and the truncated
   flag are filled here from the search result.  Also renders the why-not
   report and the derive/why-not envelopes. *\

(set capcov.*certificate-version* "capcov-static-certificate-v1")

\\ ---------------------------------------------------------------- derivation nodes

(define capcov.node-json
  [capcov.fact Rel Row Ids] ->
    (capcov.obj [(@p "kind" "fact") (@p "relation" Rel) (@p "row" (capcov.arr Row)) (@p "evidence" (capcov.arr Ids))])
  [capcov.rule-app Name Digest Rel Row Premises Neg Cmp] ->
    (capcov.obj [(@p "kind" "rule") (@p "rule" Name) (@p "rule_digest" Digest)
                 (@p "relation" Rel) (@p "row" (capcov.arr Row))
                 (@p "premises" (capcov.arr (map (function capcov.node-json) Premises)))
                 (@p "absent" (capcov.arr (map (function capcov.absent-json) Neg)))
                 (@p "comparisons" (capcov.arr (map (function capcov.comparison-record-json) Cmp)))]))

(define capcov.absent-json
  [Rel Row] -> (capcov.obj [(@p "relation" Rel) (@p "row" (capcov.arr Row))]))

(define capcov.comparison-record-json
  [L Op R] -> (capcov.obj [(@p "left" L) (@p "operator" Op) (@p "right" R)]))

\\ ---------------------------------------------------------------- leaves, witnesses, absent

\* capcov.collect walks the node tree once and accumulates
   (@p Leaves (@p Witnesses Absent)) where Leaves is a list of evidence ids,
   Witnesses a list of (@p Key Obj) for completeness/compatibility leaves and
   Absent a list of (@p Key Obj) for negated rows, Key being Python's
   canonical_json((relation, row)). *\

(define capcov.collect
  [capcov.fact Rel Row Ids] Acc ->
    (let Decl (capcov.decl Rel)
         Modality (if (= Decl capcov.none) "" (capcov.decl-modality Decl))
         Leaves (append Ids (fst Acc))
         Witnesses (if (element? Modality ["completeness" "compatibility"])
                       [(@p (capcov.json-of [Rel Row])
                            (capcov.obj [(@p "relation" Rel) (@p "row" (capcov.arr Row)) (@p "modality" Modality) (@p "evidence" (capcov.arr Ids))]))
                        | (fst (snd Acc))]
                       (fst (snd Acc)))
      (@p Leaves (@p Witnesses (snd (snd Acc)))))
  [capcov.rule-app _ _ _ _ Premises Neg _] Acc ->
    (let Absent (append (map (/. N (@p (capcov.json-of N) (capcov.absent-json N))) Neg) (snd (snd Acc)))
      (capcov.collect-all Premises (@p (fst Acc) (@p (fst (snd Acc)) Absent)))))

(define capcov.collect-all
  [] Acc -> Acc
  [N | Ns] Acc -> (capcov.collect-all Ns (capcov.collect N Acc)))

(define capcov.unique-by-key
  \\ keep one object per key, canonical key order (Python: sorted(dict))
  Pairs -> (map (function snd) (capcov.dedupe-keys (capcov.sort-by-key (function fst) Pairs))))

(define capcov.dedupe-keys
  [] -> []
  [P] -> [P]
  [P Q | R] -> (if (= (fst P) (fst Q)) (capcov.dedupe-keys [Q | R]) [P | (capcov.dedupe-keys [Q | R])]))

\\ ---------------------------------------------------------------- the certificate

(define capcov.certificate-base
  Rel Row -> [(@p "certificate_version" (value capcov.*certificate-version*))
              (@p "bundle_digest" (nth 1 (value capcov.*in-digests*)))
              (@p "rules_digest" (nth 2 (value capcov.*in-digests*)))
              (@p "bounds" (capcov.obj [(@p "max_depth" (value capcov.*max-depth*)) (@p "max_nodes" (value capcov.*max-nodes*))]))
              (@p "conclusion" (capcov.obj [(@p "relation" Rel) (@p "row" (capcov.arr Row))]))])

(define capcov.certificate-json
  Rel Row Node -> (let Acc (capcov.collect Node (@p [] (@p [] [])))
                       Leaves (capcov.dedupe (capcov.sort-strings (fst Acc)))
                       Witnesses (capcov.unique-by-key (fst (snd Acc)))
                       Absent (capcov.unique-by-key (snd (snd Acc)))
                    (capcov.obj (append (capcov.certificate-base Rel Row)
                                        [(@p "truncated" false) (@p "truncation" capcov.null)
                                         (@p "steps" (value capcov.*max-steps*))
                                         (@p "nodes" (value capcov.*nodes*))
                                         (@p "nesting" (value capcov.*max-nesting*))
                                         (@p "derivation" (capcov.node-json Node))
                                         (@p "leaves" (capcov.arr Leaves))
                                         (@p "witnesses" (capcov.arr Witnesses))
                                         (@p "absent" (capcov.arr Absent))]))))

(define capcov.truncated-certificate-json
  Rel Row Reason -> (capcov.obj (append (capcov.certificate-base Rel Row)
                                        [(@p "truncated" true) (@p "truncation" Reason)
                                         (@p "steps" capcov.null)
                                         (@p "nodes" (value capcov.*nodes*))
                                         (@p "derivation" capcov.null)
                                         (@p "leaves" (capcov.arr []))
                                         (@p "witnesses" (capcov.arr []))
                                         (@p "absent" (capcov.arr []))])))

\\ ---------------------------------------------------------------- envelopes

(define capcov.why-not-report
  Rel Row -> (let Present (capcov.present? Rel Row)
                  Alternatives (if Present [] (capcov.why-not Rel Row))
               (capcov.obj [(@p "relation" Rel) (@p "row" (capcov.arr Row))
                            (@p "present" Present)
                            (@p "alternatives" (capcov.arr Alternatives))
                            (@p "truncated" (if Present false (value capcov.*wn-truncated*)))
                            (@p "truncation" (if Present capcov.null (value capcov.*wn-reason*)))])))

(define capcov.search
  \\ [capcov.found Node] | [capcov.truncated Reason]; other errors propagate
  Rel Row -> (trap-error [capcov.found (capcov.derive Rel Row 0)]
                         (/. E (if (capcov.prefix? "capcov-truncated: " (error-to-string E))
                                   [capcov.truncated (capcov.str-after (error-to-string E) 18)]
                                   (error (error-to-string E))))))

(define capcov.derive-json
  Rel Row -> (if (not (capcov.present? Rel Row))
                 (do (set capcov.*phase* capcov.why-not)
                     (capcov.json-chunks (capcov.obj [(@p "kind" "derive") (@p "outcome" "negative")
                                                  (@p "certificate" capcov.null)
                                                  (@p "why_not" (capcov.why-not-report Rel Row))
                                                  (@p "elaborated_checksum" (value capcov.*elaborated-checksum*))
                                                  (@p "work" (value capcov.*work*))])))
                 (let Result (capcov.search Rel Row)
                   (capcov.json-chunks
                     (capcov.obj [(@p "kind" "derive")
                                  (@p "outcome" (if (= (hd Result) capcov.found) "positive" "truncated"))
                                  (@p "certificate" (if (= (hd Result) capcov.found)
                                                        (capcov.certificate-json Rel Row (nth 2 Result))
                                                        (capcov.truncated-certificate-json Rel Row (nth 2 Result))))
                                  (@p "why_not" capcov.null)
                                  (@p "elaborated_checksum" (value capcov.*elaborated-checksum*))
                                  (@p "work" (value capcov.*work*))])))))

(define capcov.why-not-json
  Rel Row -> (capcov.json-chunks (capcov.obj [(@p "kind" "why-not")
                                          (@p "report" (capcov.why-not-report Rel Row))
                                          (@p "elaborated_checksum" (value capcov.*elaborated-checksum*))
                                          (@p "work" (value capcov.*work*))])))
