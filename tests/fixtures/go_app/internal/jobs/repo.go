package jobs

import (
	"github.com/example/jobsvc/gorm"
	"github.com/example/jobsvc/models"
)

type Repo struct {
	db *gorm.DB
}

// Get reads a Job (gorm First). The op_query captures the &models.Job{}
// construction; SCIP resolves that this method is reached from GetJob.
func (r *Repo) Get() {
	r.db.First(&models.Job{})
}

// Write creates an AuditLog (gorm Create).
func (r *Repo) Write() {
	r.db.Create(&models.AuditLog{})
}
