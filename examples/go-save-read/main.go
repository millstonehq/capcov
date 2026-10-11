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

func handle(pattern string, handler http.HandlerFunc) { http.HandleFunc(pattern, handler) }

func createNote(w http.ResponseWriter, r *http.Request) {
	mu.Lock()
	defer mu.Unlock()
	notes = append(notes, r.FormValue("title"))
	w.WriteHeader(http.StatusCreated)
	fmt.Fprint(w, "created")
}

func listNotes(w http.ResponseWriter, _ *http.Request) {
	mu.Lock()
	defer mu.Unlock()
	fmt.Fprint(w, strings.Join(notes, "\n"))
}

func main() {
	handle("POST /notes/create", createNote)
	handle("GET /notes", listNotes)
	if err := http.ListenAndServe("127.0.0.1:"+os.Getenv("PORT"), nil); err != nil {
		panic(err)
	}
}
