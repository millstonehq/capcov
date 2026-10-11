package api

import "github.com/example/jobsvc/internal/service"

var svc = &service.Service{}

// GetJob is the route handler. It touches no data directly; the fixpoint must
// trace it across packages and files down to the entities Repo.Get/Repo.Write
// touch.
func GetJob(w int, r int) {
	svc.Fetch()
}

// Register mounts the route. `handle` is the config-supplied route shape the
// tree-sitter query recognizes.
func Register() {
	handle("GET /jobs/{id}", GetJob)
}

func handle(pattern string, fn func(int, int)) {}
