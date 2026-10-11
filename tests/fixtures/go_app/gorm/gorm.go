// Package gorm is a tiny local stand-in for jinzhu/gorm, so the fixture is a
// self-contained, offline-buildable module: `gorm.Model` embedding and the
// `First`/`Create` data-access methods exist without a network dependency, and
// the tree-sitter entity/op queries see the exact source tokens a real gorm app
// shows (`gorm.Model`, `db.First(&Job{})`).
package gorm

type Model struct {
	ID uint
}

type DB struct{}

func (db *DB) First(dest interface{}) *DB  { return db }
func (db *DB) Create(value interface{}) *DB { return db }
