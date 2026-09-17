\* [Id Op Rule RuleIndex Endpoints Reach Witness]: the entry names a rule the
   model indexes and an endpoint the atlas knows, and both of its dispatch
   predicates run on the witness op without raising. *\
(datatype wf-registry
  if (element? Rule (tt.value RI))
  if (element? Op (tt.value Es))
  if (= R dispatches)
  if (= W dispatches)
  ______________________
  (cons Id (cons Op (cons Rule (cons RI (cons Es (cons R (cons W []))))))) : wf-registry;)
