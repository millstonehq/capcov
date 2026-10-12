package main

import (
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
)

var (
	mu    sync.Mutex
	notes []string
)

func route(pattern string, handler http.HandlerFunc) { http.HandleFunc(pattern, handler) }

func main() {
	route("GET /notes", listNotes)
	route("POST /notes", createNote)
	route("POST /notes/{id}/archive", archiveNote)
	http.ListenAndServe("127.0.0.1:"+os.Getenv("PORT"), nil)
}

func listNotes(w http.ResponseWriter, _ *http.Request) {
	mu.Lock()
	defer mu.Unlock()
	fmt.Fprint(w, strings.Join(notes, "\n"))
}

func createNote(w http.ResponseWriter, r *http.Request) {
	mu.Lock()
	defer mu.Unlock()
	notes = append(notes, r.FormValue("title"))
	w.WriteHeader(http.StatusCreated)
}

func archiveNote(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintf(w, "archived %s", r.PathValue("id"))
}
