\* model_min/shen/base.shen -- list helpers, state accessors, the atlas. *\

(define norn.filter
  _ [] -> []
  F [X | Xs] -> [X | (norn.filter F Xs)] where (F X)
  F [_ | Xs] -> (norn.filter F Xs))

(define norn.any?
  _ [] -> false
  F [X | _] -> true where (F X)
  F [_ | Xs] -> (norn.any? F Xs))

(define norn.facts
  [abs Fs] -> Fs)

(define norn.issue-live?
  I [[issue I _ _ active] | _] -> true
  I [_ | Fs] -> (norn.issue-live? I Fs)
  _ [] -> false)

(define norn.issue-proj
  I [[issue I _ _ St] | _] -> St
  I [_ | Fs] -> (norn.issue-proj I Fs)
  _ [] -> deleted)

(define norn.child-of?
  I [comment _ I] -> true
  I [file _ I _ _] -> true
  _ _ -> false)

(define norn.equipment-of
  I [[issue-equipment I E] | Fs] -> [E | (norn.equipment-of I Fs)]
  I [_ | Fs] -> (norn.equipment-of I Fs)
  _ [] -> [])

(define norn.atlas.atomicity
  -> [[txn delete-issue observed atomic [src "controller.php" 1]]
      [txn delete-issue required non-atomic [src "model.php" 2]]
      [txn add-comment observed atomic [src "controller.php" 3]]
      [txn add-comment required atomic [src "controller.php" 3]]])

(define norn.atlas.failure-policy
  -> [[on-failure delete-issue fail-stop [src "controller.php" 1]]
      [on-failure add-comment fail-stop [src "controller.php" 3]]])

(define norn.atlas.op-name
  [delete-issue _ _] -> delete-issue
  [add-comment _ _ _] -> add-comment
  Op -> (error (make-string "model_min: unknown op ~A" Op)))

(define norn.atlas.lookup
  V E [] -> (error (make-string "model_min: no ~A atomicity fact for endpoint ~A" V E))
  V E [[txn E V A _] | _] -> A
  V E [_ | Rs] -> (norn.atlas.lookup V E Rs))

(define norn.atomic?
  Op -> (= atomic (norn.atlas.lookup observed (norn.atlas.op-name Op) (norn.atlas.atomicity))))

(define norn.atlas.failure
  E [] -> (error (make-string "model_min: no failure policy for endpoint ~A" E))
  E [[on-failure E F _] | _] -> F
  E [_ | Rs] -> (norn.atlas.failure E Rs))
