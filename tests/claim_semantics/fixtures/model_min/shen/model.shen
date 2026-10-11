\* model_min/shen/model.shen -- step, effects, as-is effects, admissibility,
   declared writes, registry, rule index. *\

(define norn.mark-deleted
  _ [] -> []
  I [[issue I P Co _] | Fs] -> [[issue I P Co deleted] | (norn.mark-deleted I Fs)]
  I [F | Fs] -> [F | (norn.mark-deleted I Fs)])

\* INTENDED: mark the issue deleted and remove its children. *\
(define norn.step
  [abs Fs] [delete-issue _ I] -> [abs (norn.mark-deleted I (norn.filter (/. F (not (norn.child-of? I F))) Fs))]
  [abs Fs] [add-comment _ I C] -> [abs [[comment C I] | Fs]]
  St _ -> St)

(define norn.stats-effects
  I Fs -> (map (/. E [stats-recompute E]) (norn.equipment-of I Fs)))

\* The intended effect list: child deletes, the parent flip, statistics, audit. *\
(define norn.effects
  [abs Before] _ [delete-issue _ I]
    -> (append (map (/. F (norn.child-delete F)) (norn.filter (/. F (norn.child-of? I F)) Before))
               (append [[sql-update "issue" I [[state -1]]]]
                       (append (norn.stats-effects I Before) [[audit issue-deleted I]])))
  _ _ [add-comment _ I C] -> [[sql-insert "comment" [[id C] [issue_id I]]] [audit comment-added C]]
  _ _ Op -> (error (make-string "model_min: no effects for ~A" Op)))

(define norn.child-delete
  [comment C _] -> [sql-delete "comment" C]
  [file F _ _ _] -> [sql-delete "file" F])

\* AS DEPLOYED: the cascade does not run (registry d-01), so only the flip,
   statistics and audit remain.  Live targets only. *\
(define norn.effects-as-is
  St [delete-issue A I]
    -> (norn.filter (/. E (not (norn.omitted? E))) (norn.effects St (norn.step St [delete-issue A I]) [delete-issue A I]))
       where (norn.issue-live? I (norn.facts St))
  _ [delete-issue _ I] -> (error (make-string "model_min: issue ~A is not live" I))
  _ Op -> (error (make-string "model_min: no as-is target for ~A" (norn.atlas.op-name Op))))

(define norn.omitted?
  [sql-delete "comment" _] -> true
  [sql-delete "file" _] -> true
  _ -> false)

(define norn.apply-effects
  St [] -> St
  [abs Fs] [[sql-update "issue" I _] | Es] -> (norn.apply-effects [abs (norn.mark-deleted I Fs)] Es)
  St [_ | Es] -> (norn.apply-effects St Es))

(define norn.successor-as-is
  St Op -> (norn.apply-effects St (norn.effects-as-is St Op)))

(define norn.admissible-as-is
  St Op Out -> (error (make-string "model_min: op ~A is not observed-atomic" (norn.atlas.op-name Op)))
               where (not (norn.atomic? Op))
  _ _ Out -> (error (make-string "model_min: unknown outcome ~A" Out))
             where (not (element? Out [committed aborted unknown]))
  St [delete-issue A I] committed -> [(norn.successor-as-is St [delete-issue A I])]
  St [delete-issue _ _] aborted -> [St]
  St [delete-issue A I] unknown -> [St (norn.successor-as-is St [delete-issue A I])]
                                   where (norn.issue-live? I (norn.facts St))
  St [delete-issue _ _] unknown -> [St]
  _ Op _ -> (error (make-string "model_min: no as-is target for ~A" (norn.atlas.op-name Op))))

(define norn.writes
  [delete-issue _ _] -> ["entity_statistics" "issue" "mongo:issue"]
  Op -> (error (make-string "model_min: no declared write-set for ~A" (norn.atlas.op-name Op))))

(define norn.known-divergences
  -> [[kd d-01 delete-issue no-orphan-child target-with-children children-of-target
        "the cascade does not run" [src "model.php" 2]]])

(define norn.kd.reachable?
  target-with-children [delete-issue _ I] Before After
    -> (and (= deleted (norn.issue-proj I (norn.facts After)))
            (norn.any? (/. F (norn.child-of? I F)) (norn.facts Before)))
  P _ _ _ -> (error (make-string "model_min: unknown reachability predicate ~A" P)))

(define norn.kd.witnesses
  children-of-target [delete-issue _ I] St -> (norn.filter (/. F (norn.child-of? I F)) (norn.facts St))
  G _ _ -> (error (make-string "model_min: unknown witness generator ~A" G)))

(define norn.rule-index
  -> [no-orphan-child no-leaked-blob])
