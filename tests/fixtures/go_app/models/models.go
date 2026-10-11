package models

import "github.com/example/jobsvc/gorm"

// Job is a gorm model: the entity_query recognizes the embedded gorm.Model and
// the recognizer derives the table `jobs` (snake_case plural, no TableName).
type Job struct {
	gorm.Model
	Title string
}

// AuditLog derives the table `audit_logs`.
type AuditLog struct {
	gorm.Model
	Action string
}
