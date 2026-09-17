\* [Endpoint Observed Required Failure]: both atomicity facts and the failure
   policy exist and take documented values. *\
(datatype wf-atlas
  if (element? Ob [atomic non-atomic eventual])
  if (element? Rq [atomic non-atomic eventual])
  if (element? F [fail-stop best-effort])
  ______________________
  (cons E (cons Ob (cons Rq (cons F [])))) : wf-atlas;)
