package service

import "github.com/example/jobsvc/internal/jobs"

type Service struct {
	repo *jobs.Repo
}

// Fetch is the intermediate hop: it touches no data itself, it calls the repo.
// This is what makes the resolved chain GetJob -> Service.Fetch -> Repo.Get a
// two-hop (call-chain:2) capability rather than a direct touch.
func (s *Service) Fetch() {
	s.repo.Get()
	s.repo.Write()
}
