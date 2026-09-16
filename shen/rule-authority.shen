\* capcov rule authority (EXPERIMENT-PLAN section 18, Stage D).

   Structural authority checks over the elaborated rule pack produced by
   claim-workbench.shen, plus the freeze of that elaborated representation.
   Every check has a stable id; a verdict is emitted per rule and per pack.

   Per-rule check ids
     undeclared-relation                   an atom names a relation without a
                                           declaration (treated as an unapproved
                                           effectful side condition)
     ungrounded-conclusion-variable        a head variable no positive premise
                                           (or explicit aggregation) binds
     ungrounded-side-condition-variable    a negated-atom or comparison variable
                                           no positive premise binds
     context-index-loss                    the head drops or renames a context
                                           index bound in the body without an
                                           explicit compatibility witness
     unsupported-context-widening          two premises bind one context index
                                           to different terms without a
                                           compatibility witness naming both
     negative-conclusion-without-completeness
                                           a negated premise has no exact scoped
                                           completeness witness of its context
     declaration-promoted-to-effect        a runtime-effect conclusion whose
                                           only positive premises are static
                                           declarations or assumptions
     non-linear-recursion                  a recursive rule with two premises
                                           in its own component (unsupported
                                           by the bounded search)
   Pack-level check ids
     stratified-negation                   negation is stratifiable
     frozen-pack-mismatch                  the elaborated pack's checksum
                                           equals the frozen checksum supplied
                                           (a mismatch is a refusal, raised
                                           before any request is served)

   These checks are structural: they inspect declarations and rule shapes,
   never evidence.  They are deliberately no stricter than the Python
   validator's unsafe-variable / missing-compatibility / missing-completeness
   / mixed-binding-join family, whose typed-position witness matching they
   approximate by target-set membership. *\

\\ ---------------------------------------------------------------- elaborated pack (frozen representation)

(define capcov.column-json
  [Name Type Flag] -> (capcov.obj [(@p "context" Flag) (@p "name" Name) (@p "type" Type)]))

(define capcov.decl-json
  D -> (capcov.obj [(@p "binding" (capcov.decl-binding D))
                    (@p "columns" (capcov.arr (map (function capcov.column-json) (capcov.decl-columns D))))
                    (@p "compatibility_context_indices" (capcov.arr (capcov.decl-compat-context D)))
                    (@p "compatibility_targets" (capcov.arr (capcov.decl-compat-targets D)))
                    (@p "completes" (capcov.decl-completes D))
                    (@p "context_indices" (capcov.arr (capcov.decl-context D)))
                    (@p "modality" (capcov.decl-modality D))
                    (@p "name" (capcov.decl-name D))
                    (@p "polarity" (capcov.decl-polarity D))
                    (@p "primitive" (capcov.decl-primitive? D))]))

(define capcov.kind-name
  capcov.plain -> "plain"
  capcov.base -> "base"
  capcov.step -> "step"
  capcov.nonlinear -> "nonlinear")

(define capcov.elaborated-rule-json
  R -> (capcov.obj [(@p "name" (capcov.rule-name R))
                    (@p "rule_digest" (capcov.rule-digest R))
                    (@p "canonical" (capcov.json-quote (capcov.rule-key R)))
                    (@p "head" (capcov.atom-json false (capcov.rule-head-rel R) (capcov.rule-head-terms R)))
                    (@p "body" (capcov.arr (map (function capcov.body-item-json) (capcov.rule-body R))))
                    (@p "kind" (capcov.kind-name (capcov.rule-kind R)))
                    (@p "recursive_premises" (capcov.arr (capcov.rule-recursive-atoms R)))
                    (@p "stratum" (capcov.rule-stratum R))]))

(define capcov.elaborated-json
  -> (capcov.obj [(@p "elaboration" "capcov-shen-elaborated-v1")
                  (@p "relations" (capcov.arr (map (/. N (capcov.decl-json (capcov.decl N))) (value capcov.*relation-names*))))
                  (@p "rules" (capcov.arr (map (function capcov.elaborated-rule-json) (value capcov.*rules*))))
                  (@p "stratified" (not (value capcov.*unstratifiable*)))]))

(define capcov.freeze-pack
  \\ render the elaborated representation once, fingerprint it, and refuse to
  \\ serve any request when a frozen fingerprint was supplied and differs
  -> (let Chunks (capcov.json-chunks (capcov.elaborated-json))
          Sum (capcov.checksum-chunks Chunks)
       (do (set capcov.*elaborated-chunks* Chunks)
           (set capcov.*elaborated-checksum* Sum)
           (if (or (= (value capcov.*in-frozen*) capcov.null) (= (value capcov.*in-frozen*) Sum))
               ok
               (error "capcov-frozen-mismatch: elaborated pack checksum ~A differs from frozen ~A" Sum (value capcov.*in-frozen*))))))

\\ ---------------------------------------------------------------- rule accessors for the checks

(define capcov.positive-atoms
  [] -> []
  [[capcov.pos Rel Terms] | Rest] -> [[Rel Terms] | (capcov.positive-atoms Rest)]
  [_ | Rest] -> (capcov.positive-atoms Rest))

(define capcov.negative-atoms
  [] -> []
  [[capcov.neg Rel Terms] | Rest] -> [[Rel Terms] | (capcov.negative-atoms Rest)]
  [_ | Rest] -> (capcov.negative-atoms Rest))

(define capcov.all-atoms
  [] -> []
  [[capcov.pos Rel Terms] | Rest] -> [[Rel Terms] | (capcov.all-atoms Rest)]
  [[capcov.neg Rel Terms] | Rest] -> [[Rel Terms] | (capcov.all-atoms Rest)]
  [_ | Rest] -> (capcov.all-atoms Rest))

(define capcov.comparison-vars
  [] -> []
  [[capcov.cmp L Op R] | Rest] -> (append (capcov.term-vars [L R]) (capcov.comparison-vars Rest))
  [_ | Rest] -> (capcov.comparison-vars Rest))

(define capcov.declared-atoms
  Atoms -> (capcov.filter (/. A (not (= (capcov.decl (nth 1 A)) capcov.none))) Atoms))

(define capcov.atom-term-at
  \\ the term bound to column Name in atom [Rel Terms], or capcov.none
  [Rel Terms] Name -> (let I (capcov.index-of Name (capcov.column-names (capcov.decl Rel)))
                        (if (= I capcov.none) capcov.none (capcov.nth0 I Terms))))

(define capcov.atom-modality
  [Rel _] -> (capcov.decl-modality (capcov.decl Rel)))

(define capcov.atom-binding
  [Rel _] -> (capcov.decl-binding (capcov.decl Rel)))

(define capcov.compat-witnesses
  Rule -> (capcov.filter (/. A (= (capcov.atom-modality A) "compatibility"))
                         (capcov.declared-atoms (capcov.positive-atoms (capcov.rule-body Rule)))))

(define capcov.term-text
  [capcov.v Name] -> Name
  [capcov.c C T] -> (capcov.json-of C))

(define capcov.names-text
  Names -> (capcov.join (capcov.intersperse ", " Names)))

(define capcov.check
  Id Ok Detail -> (capcov.obj [(@p "id" Id) (@p "ok" Ok) (@p "detail" Detail)]))

(define capcov.check-ok?
  [capcov.obj (@p "id" _) (@p "ok" Ok) (@p "detail" _)] -> Ok)

(define capcov.minus
  [] _ -> []
  [X | Xs] L -> (if (element? X L) (capcov.minus Xs L) [X | (capcov.minus Xs L)]))

\\ ---------------------------------------------------------------- per-rule checks

(define capcov.check-undeclared
  Rule -> (let Names (capcov.dedupe (capcov.sort-strings [(capcov.rule-head-rel Rule) | (capcov.body-relations (capcov.rule-body Rule))]))
               Missing (capcov.filter (/. N (= (capcov.decl N) capcov.none)) Names)
            (if (empty? Missing)
                (capcov.check "undeclared-relation" true "every relation in the rule is declared")
                (capcov.check "undeclared-relation" false (cn "undeclared (unapproved effectful side condition): " (capcov.names-text Missing))))))

(define capcov.positive-vars
  Rule -> (let Vars (capcov.dedupe (capcov.sort-strings (capcov.join-vars (capcov.positive-atoms (capcov.rule-body Rule)))))
               Agg (capcov.rule-aggregation Rule)
            (if (= Agg capcov.null) Vars [(nth 1 Agg) | Vars])))

(define capcov.join-vars
  [] -> []
  [[Rel Terms] | Rest] -> (append (capcov.term-vars Terms) (capcov.join-vars Rest)))

(define capcov.check-head-grounding
  Rule -> (let Bound (capcov.positive-vars Rule)
               Missing (capcov.dedupe (capcov.sort-strings (capcov.minus (capcov.term-vars (capcov.rule-head-terms Rule)) Bound)))
            (if (empty? Missing)
                (capcov.check "ungrounded-conclusion-variable" true "every head variable is bound by a positive premise")
                (capcov.check "ungrounded-conclusion-variable" false (cn "head variables without a positive premise: " (capcov.names-text Missing))))))

(define capcov.check-side-grounding
  Rule -> (let Bound (capcov.positive-vars Rule)
               Side (append (capcov.join-vars (capcov.negative-atoms (capcov.rule-body Rule))) (capcov.comparison-vars (capcov.rule-body Rule)))
               Missing (capcov.dedupe (capcov.sort-strings (capcov.minus Side Bound)))
            (if (empty? Missing)
                (capcov.check "ungrounded-side-condition-variable" true "negation and comparison variables are positively bound")
                (capcov.check "ungrounded-side-condition-variable" false (cn "side-condition variables without a positive premise: " (capcov.names-text Missing))))))

(define capcov.carriers
  \\ declared positive premises whose relation declares context index C
  C Atoms -> (capcov.filter (/. A (element? C (capcov.decl-context (capcov.decl (nth 1 A))))) Atoms))

(define capcov.context-bound?
  \\ is head context term T for index C bound by a carrier's own C column?
  C T Carriers -> (capcov.any? (/. A (= (capcov.atom-term-at A C) T)) Carriers))

(define capcov.witness-carries?
  T Witnesses -> (capcov.any? (/. W (element? T (nth 2 W))) Witnesses))

(define capcov.context-carried?
  \\ a head context variable must be carried by a premise's same index (or a
  \\ compatibility witness); a head context constant is an explicit
  \\ constructor, accepted unless a premise binds that index to something else
  C T Pos Witnesses -> (let Carriers (capcov.carriers C Pos)
                         (if (capcov.variable? T)
                             (or (capcov.context-bound? C T Carriers) (capcov.witness-carries? T Witnesses))
                             (or (empty? Carriers) (or (capcov.context-bound? C T Carriers) (capcov.witness-carries? T Witnesses))))))

(define capcov.check-context-loss
  Rule -> (let HD (capcov.decl (capcov.rule-head-rel Rule))
            (if (= HD capcov.none)
                (capcov.check "context-index-loss" false "head relation is undeclared")
                (let Head [(capcov.rule-head-rel Rule) (capcov.rule-head-terms Rule)]
                     Pos (capcov.declared-atoms (capcov.positive-atoms (capcov.rule-body Rule)))
                     Witnesses (capcov.compat-witnesses Rule)
                     Lost (capcov.filter (/. C (not (capcov.context-carried? C (capcov.atom-term-at Head C) Pos Witnesses)))
                                         (capcov.decl-context HD))
                  (if (empty? Lost)
                      (capcov.check "context-index-loss" true "every head context index is carried from a premise of the same index or fixed by an explicit constant")
                      (capcov.check "context-index-loss" false (cn "head context indices not carried from any premise's same index (no compatibility witness): " (capcov.names-text Lost))))))))

(define capcov.pairs-of
  [] -> []
  [X | Xs] -> (append (map (/. Y (@p X Y)) Xs) (capcov.pairs-of Xs)))

(define capcov.shared-context
  A B -> (capcov.filter (/. C (element? C (capcov.decl-context (capcov.decl (nth 1 B)))))
                        (capcov.decl-context (capcov.decl (nth 1 A)))))

(define capcov.differing-context
  A B -> (capcov.filter (/. C (not (= (capcov.atom-term-at A C) (capcov.atom-term-at B C)))) (capcov.shared-context A B)))

(define capcov.witnessed-pair?
  A B Witnesses -> (capcov.any? (/. W (let Targets (capcov.decl-compat-targets (capcov.decl (nth 1 W)))
                                        (and (element? (nth 1 A) Targets) (element? (nth 1 B) Targets))))
                                Witnesses))

(define capcov.check-widening
  Rule -> (let Atoms (capcov.declared-atoms (capcov.all-atoms (capcov.rule-body Rule)))
               Witnesses (capcov.compat-witnesses Rule)
               Bad (capcov.filter (/. P (and (not (empty? (capcov.differing-context (fst P) (snd P))))
                                             (not (capcov.witnessed-pair? (fst P) (snd P) Witnesses))))
                                  (capcov.pairs-of Atoms))
            (if (empty? Bad)
                (capcov.check "unsupported-context-widening" true "premises agree on every shared context index or carry a compatibility witness")
                (capcov.check "unsupported-context-widening" false
                              (cn "premises join different values of a shared context index without a compatibility witness: "
                                  (capcov.names-text (map (/. P (cn (nth 1 (fst P)) (cn "/" (cn (nth 1 (snd P)) (cn " on " (capcov.names-text (capcov.differing-context (fst P) (snd P)))))))) Bad)))))))

(define capcov.completeness-witness?
  \\ Python _validate_completeness: same completes target, same context indices, shared columns bound identically
  Neg W -> (let TD (capcov.decl (nth 1 Neg)) WD (capcov.decl (nth 1 W))
             (and (= (capcov.decl-modality WD) "completeness")
                  (and (= (capcov.decl-completes WD) (nth 1 Neg))
                       (and (= (capcov.decl-context WD) (capcov.decl-context TD))
                            (capcov.all? (/. Name (or (= (capcov.index-of Name (capcov.column-names WD)) capcov.none)
                                                      (= (capcov.atom-term-at Neg Name) (capcov.atom-term-at W Name))))
                                         (capcov.column-names TD)))))))

(define capcov.check-completeness
  Rule -> (let Negs (capcov.declared-atoms (capcov.negative-atoms (capcov.rule-body Rule)))
               Pos (capcov.declared-atoms (capcov.positive-atoms (capcov.rule-body Rule)))
               Bad (capcov.filter (/. N (not (capcov.any? (/. W (capcov.completeness-witness? N W)) Pos))) Negs)
            (cond ((empty? Negs) (capcov.check "negative-conclusion-without-completeness" true "no negated premise"))
                  ((empty? Bad) (capcov.check "negative-conclusion-without-completeness" true "every negated premise has an exact scoped completeness witness"))
                  (true (capcov.check "negative-conclusion-without-completeness" false
                                      (cn "negated premises without an exact scoped completeness witness: " (capcov.names-text (map (/. N (nth 1 N)) Bad))))))))

(define capcov.effective-premise?
  A -> (and (= (capcov.atom-binding A) "runtime")
            (not (element? (capcov.atom-modality A) ["compatibility" "completeness" "assumption"]))))

(define capcov.check-promotion
  Rule -> (let HD (capcov.decl (capcov.rule-head-rel Rule))
            (cond ((= HD capcov.none) (capcov.check "declaration-promoted-to-effect" false "head relation is undeclared"))
                  ((not (and (= (capcov.decl-binding HD) "runtime") (element? (capcov.decl-modality HD) ["derived" "claim"])))
                   (capcov.check "declaration-promoted-to-effect" true "conclusion is not a runtime effect"))
                  ((capcov.any? (function capcov.effective-premise?) (capcov.declared-atoms (capcov.positive-atoms (capcov.rule-body Rule))))
                   (capcov.check "declaration-promoted-to-effect" true "a runtime observation or derivation supports the runtime conclusion"))
                  (true (capcov.check "declaration-promoted-to-effect" false "runtime effect concluded from static declarations or assumptions only")))))

(define capcov.check-linear
  Rule -> (if (= (capcov.rule-kind Rule) capcov.nonlinear)
              (capcov.check "non-linear-recursion" false "rule has more than one premise in its own recursive component")
              (capcov.check "non-linear-recursion" true (cn "rule kind " (capcov.kind-name (capcov.rule-kind Rule))))))

(define capcov.rule-verdict
  Rule -> (let Checks [(capcov.check-undeclared Rule)
                       (capcov.check-head-grounding Rule)
                       (capcov.check-side-grounding Rule)
                       (capcov.check-context-loss Rule)
                       (capcov.check-widening Rule)
                       (capcov.check-completeness Rule)
                       (capcov.check-promotion Rule)
                       (capcov.check-linear Rule)]
            (capcov.obj [(@p "rule" (capcov.rule-name Rule))
                         (@p "rule_digest" (capcov.rule-digest Rule))
                         (@p "ok" (capcov.all? (function capcov.check-ok?) Checks))
                         (@p "checks" (capcov.arr Checks))])))

(define capcov.verdict-ok?
  [capcov.obj (@p "rule" _) (@p "rule_digest" _) (@p "ok" Ok) (@p "checks" _)] -> Ok)

\\ ---------------------------------------------------------------- pack-level checks and output

(define capcov.pack-checks
  -> [(if (value capcov.*unstratifiable*)
          (capcov.check "stratified-negation" false "negation through recursion has no stratification")
          (capcov.check "stratified-negation" true "negation is stratified"))
      (capcov.check "frozen-pack-mismatch" true
                    (if (= (value capcov.*in-frozen*) capcov.null)
                        "no frozen checksum supplied; this run establishes one"
                        "elaborated pack checksum equals the frozen checksum"))])

(define capcov.authority-json
  -> (let Verdicts (map (function capcov.rule-verdict) (value capcov.*rules*))
          Pack (capcov.pack-checks)
       (capcov.json-chunks
         (capcov.obj [(@p "kind" "authority")
                      (@p "ok" (and (capcov.all? (function capcov.verdict-ok?) Verdicts) (capcov.all? (function capcov.check-ok?) Pack)))
                      (@p "rules" (capcov.arr Verdicts))
                      (@p "pack" (capcov.arr Pack))
                      (@p "elaborated_checksum" (value capcov.*elaborated-checksum*))
                      (@p "elaborated" (capcov.json-splice (value capcov.*elaborated-chunks*)))
                      (@p "work" (value capcov.*work*))]))))
