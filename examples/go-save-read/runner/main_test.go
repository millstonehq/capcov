package main

import (
	"os/exec"
	"testing"
	"time"
)

func TestStartRetriesExitedProcessWithoutDeadlock(t *testing.T) {
	failing, err := exec.LookPath("false")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		if process, ok := start(failing); ok || process != nil {
			t.Errorf("start(false) = (%v, %v), want (nil, false)", process, ok)
		}
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("start deadlocked after consuming the child exit notification")
	}
}
