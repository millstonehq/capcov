\* shen/modelcheck/prelude.shen -- Stage D: reify a Shen domain model for typing.

   Loaded UNTYPED (tc -) before the model.  Nothing here judges anything; it
   only (1) loads the model silently, recording every file it loaded, (2)
   computes the model's own answers to a fixed set of questions -- declared
   write-sets, the tables its effects touch on witness states, its admissible
   sets under every outcome, its atlas rows, its registry entries -- and (3)
   writes each answer as a Shen LITERAL into a generated unit file whose only
   form is a typed judgement.  The unit is then loaded under (tc +), where the
   datatypes in types/*.shen decide whether the literal inhabits the
   well-formedness type.  A type error there is a well-formedness failure of
   the model, trapped and reported as `MC FAIL <id> <message>`.

   Two facts about Shen's type checker shape everything below (measured on
   shen-go c12933d and shen-cl, plan section 18, 2026-09-16):

   * A datatype side condition sees the SYNTACTIC term being typed, so a list
     literal arrives as [cons X [cons Y []]] and must be walked (tt.value)
     before a predicate can look at it.  Side conditions on the head of a
     (cons X Xs) pattern see the literal atom directly.
   * A load unit that defines more than one function, or a bare define with
     no other form, is declared twice ("changing the type of F") and its
     judgement stops gating.  Every types/*.shen file therefore holds ONE
     datatype, or ONE define followed by a marker form, and every generated
     unit holds exactly one judgement form.

   Every predicate used by a datatype must be total: the checker tries the
   rule against unrelated terms while searching. *\

(tc -)

\* ---------- helpers over syntactic terms ---------- *\

(define tt.value
  [] -> []
  [cons X Xs] -> [(tt.value X) | (tt.value Xs)]
  X -> X)

(define tt.nodup?
  [] -> true
  [X | Xs] -> false where (element? X Xs)
  [_ | Xs] -> (tt.nodup? Xs))

(define tt.all?
  _ [] -> true
  F [X | Xs] -> (tt.all? F Xs) where (F X)
  _ _ -> false)

(define tt.subset?
  Xs Ys -> (tt.all? (/. X (element? X Ys)) Xs) where (cons? Xs)
  [] _ -> true
  _ _ -> false)

(define tt.list?
  [] -> true
  [_ | _] -> true
  _ -> false)

(define tt.filter
  _ [] -> []
  F [X | Xs] -> [X | (tt.filter F Xs)] where (F X)
  F [_ | Xs] -> (tt.filter F Xs))

(define tt.dedup
  [] -> []
  [X | Xs] -> (tt.dedup Xs) where (element? X Xs)
  [X | Xs] -> [X | (tt.dedup Xs)])

(define tt.concat
  [] -> []
  [Xs | Rest] -> (append Xs (tt.concat Rest)))

\* ---------- literal text for generated units ---------- *\

(define mc.dquote -> (n->string 34))

(define mc.q
  S -> (error "modelcheck: cannot quote a string containing a double quote")
       where (mc.has-dquote? (explode S))
  S -> (cn (mc.dquote) (cn S (mc.dquote))))

(define mc.has-dquote?
  [] -> false
  [C | Cs] -> (or (= C (mc.dquote)) (mc.has-dquote? Cs)))

(define mc.lit
  X -> (str X) where (number? X)
  X -> (mc.q X) where (string? X)
  [] -> "[]"
  X -> (cn "[" (cn (mc.lit-items X) "]")) where (cons? X)
  X -> (str X))

(define mc.lit-items
  [X] -> (mc.lit X)
  [X | Xs] -> (cn (mc.lit X) (cn " " (mc.lit-items Xs))))

\* ---------- quiet, recorded load of the model ---------- *\

(set mc.loaded [])

(define mc.quiet-load
  File -> (do (set mc.loaded (append (value mc.loaded) [File]))
              (output "MC LOADED ~A~%" File)
              (mc.quiet-eval-forms (read-file File))))

(define mc.quiet-eval-forms
  [] -> loaded
  [[load F] | Forms] -> (do (mc.quiet-load F) (mc.quiet-eval-forms Forms))
  [Form | Forms] -> (let Ignored (eval Form) (mc.quiet-eval-forms Forms)))

\* ---------- the questions asked of the model ---------- *\

\* Witness states, in the model family's documented fact shapes
   ([issue I P Co St] [issue-equipment I E] [comment C I] [file F I St B] [blob B]). *\
(define mc.witness
  f1 -> [abs [[issue 31 7 3 active]]]
  f2 -> [abs [[issue 31 7 3 active] [issue-equipment 31 61] [comment 41 31]
              [file 51 31 active "b1"] [blob "b1"] [issue 32 7 3 active]]]
  f2-no-equipment -> [abs [[issue 31 7 3 active] [comment 41 31]
                           [file 51 31 active "b1"] [blob "b1"] [issue 32 7 3 active]]]
  nonlive -> [abs [[issue 31 7 3 deleted] [comment 41 31] [issue 32 7 3 active]]])

(define mc.live-witnesses -> [f1 f2 f2-no-equipment])

\* One op term per endpoint name, targeting issue 31 as actor 9. *\
(define mc.op-term
  delete-issue -> [delete-issue 9 31]
  delete-issues -> [delete-issues 9 [31]]
  create-issue -> [create-issue 9 7 3 33]
  add-comment -> [add-comment 9 31 41]
  attach-file -> [attach-file 9 31 51 "b1"]
  edit -> [edit 9 31 memo]
  Op -> (error (make-string "modelcheck: no witness op term for endpoint ~A" Op)))

(define mc.atlas-endpoints
  -> (tt.dedup (map (/. R (mc.txn-endpoint R)) (norn.atlas.atomicity))))

(define mc.txn-endpoint
  [txn E _ _ _] -> E
  R -> (error (make-string "modelcheck: atlas row is not [txn E V A Src]: ~A" R)))

\* The table an effect writes; `none` for storage effects, which are not
   tables and are excluded from every write-set by the model's own contract. *\
(define mc.effect-table
  [sql-update T _ _] -> T
  [sql-delete T _] -> T
  [sql-insert T _] -> T
  [stats-recompute _] -> "entity_statistics"
  [audit Kind _] -> (mc.audit-table Kind)
  [s3-delete _] -> none
  [s3-put _] -> none
  E -> (error (make-string "modelcheck: not an effect: ~A" E)))

(define mc.audit-table
  issue-created -> "mongo:issue"
  issue-deleted -> "mongo:issue"
  edited -> "mongo:issue"
  comment-added -> "mongo:comment"
  file-attached -> "mongo:file"
  Kind -> (error (make-string "modelcheck: no audit lift for kind ~A" Kind)))

(define mc.tables-of
  Es -> (tt.dedup (tt.filter (/. T (not (= T none))) (map (/. E (mc.effect-table E)) Es))))

\* An answer, or the symbol `refused` when the model raised. *\
(define mc.try
  F -> (trap-error (thaw F) (/. E refused)))

(define mc.as-is-tables
  Op W -> (mc.try (freeze (mc.tables-of (norn.effects-as-is (mc.witness W) (mc.op-term Op))))))

(define mc.intended-tables
  Op W -> (mc.try (freeze (let St (mc.witness W)
                               (mc.tables-of (norn.effects St (norn.step St (mc.op-term Op)) (mc.op-term Op)))))))

(define mc.union-answers
  [] -> []
  [refused | Rest] -> (mc.union-answers Rest)
  [Ts | Rest] -> (tt.dedup (append Ts (mc.union-answers Rest))))

(define mc.judged?
  Op -> (not (= [] (tt.filter (/. W (not (= refused (mc.as-is-tables Op W)))) (mc.live-witnesses)))))

(define mc.declared
  Op -> (mc.try (freeze (norn.writes (mc.op-term Op)))))

\* [Op Declared ObservedAsIs Vocabulary] *\
(define mc.writes-row
  Op -> [Op (mc.declared Op)
            (mc.union-answers (map (/. W (mc.as-is-tables Op W)) (mc.live-witnesses)))
            (mc.union-answers (map (/. W (mc.intended-tables Op W)) (mc.live-witnesses)))])

\* [Outcome Liveness Verdict Count] for every cell of the admissibility matrix *\
(define mc.cell
  Op Out live -> (mc.cell-of Op Out live (mc.witness f2))
  Op Out nonlive -> (mc.cell-of Op Out nonlive (mc.witness nonlive)))

(define mc.cell-of
  Op Out L St -> (trap-error (let R (norn.admissible-as-is St (mc.op-term Op) Out)
                                  [Out L admits (length R)])
                             (/. E [Out L refuses 0])))

(define mc.cell-key
  [Out L _ _] -> [Out L]
  _ -> malformed)

(define mc.matrix-row
  Op -> [Op (tt.concat (map (/. Out [(mc.cell Op Out live) (mc.cell Op Out nonlive)])
                            [committed aborted unknown]))])

\* [Endpoint Observed Required Failure]; `missing` where the atlas raised *\
(define mc.atlas-row
  E -> [E (trap-error (norn.atlas.lookup observed E (norn.atlas.atomicity)) (/. X missing))
          (trap-error (norn.atlas.lookup required E (norn.atlas.atomicity)) (/. X missing))
          (trap-error (norn.atlas.failure E (norn.atlas.failure-policy)) (/. X missing))])

\* [Id Op Rule RuleIndex Endpoints ReachStatus WitnessStatus] *\
(define mc.registry-row
  [kd Id Op Rule Reach Witness _ _]
    -> [Id Op Rule (norn.rule-index) (mc.atlas-endpoints)
        (trap-error (let B (mc.witness f2)
                         A (norn.step B (mc.op-term Op))
                         Ignored (norn.kd.reachable? Reach (mc.op-term Op) B A)
                         dispatches)
                    (/. E errors))
        (trap-error (let Ignored (norn.kd.witnesses Witness (mc.op-term Op) (mc.witness f2)) dispatches)
                    (/. E errors))]
  K -> [malformed none none (norn.rule-index) (mc.atlas-endpoints) errors errors])

(define mc.registry-ids
  -> (map (/. K (mc.kd-id K)) (norn.known-divergences)))

(define mc.kd-id
  [kd Id | _] -> Id
  _ -> malformed)

\* ---------- unit generation ---------- *\

\* One form: (output "MC PASS <id> ~A~%" (<judge> <literal>)).  The argument is
   the literal; only the type checker decides whether it inhabits the type. *\
(define mc.unit-text
  Id Judge Lit -> (cn "(output " (cn (mc.q (cn "MC PASS " (cn Id " ~A~%")))
                                 (cn " (" (cn Judge (cn " " (cn Lit "))")))))))

(define mc.write-unit
  Dir Id Judge Term -> (let Path (cn Dir (cn (mc.file-safe Id) ".shen"))
                            Text (mc.unit-text Id Judge (mc.lit Term))
                            (do (write-to-file Path Text)
                                (output "MC UNIT ~A ~A~%" Id Path)
                                [Id Path])))

(define mc.file-safe
  Id -> (mc.replace-chars (explode Id)))

(define mc.replace-chars
  [] -> ""
  [":" | Cs] -> (cn "_" (mc.replace-chars Cs))
  ["/" | Cs] -> (cn "_" (mc.replace-chars Cs))
  [C | Cs] -> (cn C (mc.replace-chars Cs)))

(define mc.reify
  Dir -> (let Endpoints (mc.atlas-endpoints)
              Judged (tt.filter (/. Op (mc.judged? Op)) Endpoints)
              Skipped (tt.filter (/. Op (not (mc.judged? Op))) Endpoints)
              Ignored (map (/. Op (output "MC SKIP ~A no as-is target on any live witness~%" Op)) Skipped)
              Writes (map (/. Op (mc.write-unit Dir (cn "writes:" (str Op)) "mc.judge-writes" (mc.writes-row Op))) Judged)
              Matrix (map (/. Op (mc.write-unit Dir (cn "matrix:" (str Op)) "mc.judge-matrix" (mc.matrix-row Op))) Judged)
              Atlas (map (/. E (mc.write-unit Dir (cn "atlas:" (str E)) "mc.judge-atlas" (mc.atlas-row E))) Endpoints)
              Registry (mc.registry-units Dir (norn.known-divergences) 1)
              Ids (mc.write-unit Dir "registry-ids" "mc.judge-ids" (mc.registry-ids))
              (tt.concat [Writes Matrix Atlas Registry [Ids]])))

\* One unit per registry entry.  A repeated id is itself a defect the id-list
   judgement refuses, so the unit names stay distinct by position rather than
   letting the collision become a checker failure. *\
(define mc.registry-units
  _ [] _ -> []
  Dir [K | Ks] N -> [(mc.write-unit Dir (cn "registry:" (cn (str N) (cn ":" (str (mc.kd-id K)))))
                                    "mc.judge-registry" (mc.registry-row K))
                     | (mc.registry-units Dir Ks (+ N 1))])

\* ---------- judging ---------- *\

(define mc.judge-all
  [] -> done
  [[Id Path] | Rest] -> (do (trap-error (load Path)
                                        (/. E (output "MC FAIL ~A ~A~%" Id (error-to-string E))))
                            (mc.judge-all Rest)))
