package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

type plan struct {
	Scenarios []scenario `json:"scenarios"`
}
type scenario struct {
	ID    string `json:"id"`
	Steps []step `json:"steps"`
}
type step struct {
	Transition string    `json:"transition"`
	Commands   []command `json:"commands"`
}
type command struct {
	Op           string            `json:"op"`
	ID           string            `json:"id"`
	Mode         string            `json:"mode"`
	Method       string            `json:"method"`
	Path         string            `json:"path"`
	Text         string            `json:"text"`
	Form         map[string]string `json:"form"`
	ExpectStatus int               `json:"expect_status"`
}
type result struct {
	ID         string    `json:"id"`
	Status     string    `json:"status"`
	Assertions []string  `json:"assertions"`
	Requests   []request `json:"observed_requests"`
}
type request struct {
	Step    string `json:"step"`
	Surface string `json:"surface"`
}

func main() {
	planBytes, err := os.ReadFile(os.Getenv("CAPCOV_FLOW_PLAN"))
	must(err)
	var p plan
	must(json.Unmarshal(planBytes, &p))
	sum := sha256.Sum256(planBytes)
	fixtureRoot := os.Getenv("CAPCOV_FIXTURE_ROOT")
	if fixtureRoot == "" {
		// Empty would build the runner's OWN directory into "server" and then
		// exercise it, producing a confident observation of the wrong program.
		panic("CAPCOV_FIXTURE_ROOT must name the fixture root to build")
	}
	buildDir, err := os.MkdirTemp("", "capcov-go-save-read-")
	must(err)
	defer os.RemoveAll(buildDir)
	server := buildDir + "/server"
	build := exec.Command("go", "build", "-o", server, ".")
	build.Dir = fixtureRoot
	build.Stdout = os.Stderr
	build.Stderr = os.Stderr
	must(build.Run())
	results := make([]result, 0, len(p.Scenarios))
	allPassed := true
	for _, scenario := range p.Scenarios {
		r := runScenario(server, scenario)
		if r.Status != "passed" {
			allPassed = false
		}
		results = append(results, r)
	}
	status := "passed"
	if !allPassed {
		status = "failed"
	}
	// DECLARED, not observed: this fixture's server does not enumerate its mux,
	// so the runner cannot take a census of what is actually mounted. It is
	// reported for legibility and the exemptions file says as much; a real census
	// needs the server to enumerate its own routes.
	declaredSurfaces := []string{"http:GET /notes", "http:POST /notes/create"}
	out := map[string]any{"nonce": os.Getenv("CAPCOV_FLOW_NONCE"), "plan_sha256": hex.EncodeToString(sum[:]), "status": status, "execution_scope": "full", "mounted_surfaces": declaredSurfaces, "scenarios": results, "assurance": "real-go-http-subprocess"}
	b, err := json.Marshal(out)
	must(err)
	must(os.WriteFile(os.Getenv("CAPCOV_FLOW_OUT"), b, 0o600))
}

// A started server process and the wait that owns its exit.
type process struct {
	cmd  *exec.Cmd
	base string
	done chan error
}

// freePort asks the kernel for a port and releases it. Between the release and
// the server's bind another process can take it, which is why start() retries:
// a lost race must not be recorded as a behavioral failure.
func freePort() int {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	must(err)
	port := listener.Addr().(*net.TCPAddr).Port
	must(listener.Close())
	return port
}

func start(server string) (*process, bool) {
	for attempt := 0; attempt < 5; attempt++ {
		port := strconv.Itoa(freePort())
		cmd := exec.Command(server)
		cmd.Env = append(os.Environ(), "PORT="+port)
		cmd.Stdout = os.Stderr
		cmd.Stderr = os.Stderr
		must(cmd.Start())
		p := &process{cmd: cmd, base: "http://127.0.0.1:" + port, done: make(chan error, 1)}
		go func() { p.done <- cmd.Wait() }()
		ready, exited := p.ready()
		if ready {
			return p, true
		}
		if !exited {
			p.stop()
		}
	}
	return nil, false
}

// ready returns false the moment the process exits, so a bind failure costs one
// retry instead of the whole readiness budget.
func (p *process) ready() (ready bool, exited bool) {
	for i := 0; i < 400; i++ {
		select {
		case <-p.done:
			return false, true
		default:
		}
		if resp, e := http.Get(p.base + "/notes"); e == nil {
			_ = resp.Body.Close()
			return true, false
		}
		time.Sleep(25 * time.Millisecond)
	}
	return false, false
}

func (p *process) stop() {
	_ = p.cmd.Process.Kill()
	<-p.done
}

func runScenario(server string, s scenario) result {
	r := result{ID: s.ID, Status: "passed", Assertions: []string{}, Requests: []request{}}
	p, ok := start(server)
	if !ok {
		r.Status = "failed"
		return r
	}
	defer p.stop()
	base := p.base
	for i, st := range s.Steps {
		for _, c := range st.Commands {
			if c.Op != "assert" || c.Mode != "http" {
				r.Status = "failed"
				continue
			}
			var body io.Reader
			if c.Method == "POST" {
				vals := url.Values{}
				for k, v := range c.Form {
					vals.Set(k, v)
				}
				body = strings.NewReader(vals.Encode())
			}
			req, e := http.NewRequest(c.Method, base+c.Path, body)
			if e != nil {
				r.Status = "failed"
				continue
			}
			if c.Method == "POST" {
				req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
			}
			resp, e := http.DefaultClient.Do(req)
			if e != nil {
				r.Status = "failed"
				continue
			}
			data, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			r.Requests = append(r.Requests, request{Step: fmt.Sprintf("%d:%s", i, st.Transition), Surface: "http:" + c.Method + " " + c.Path})
			if resp.StatusCode == c.ExpectStatus && bytes.Contains(data, []byte(c.Text)) {
				r.Assertions = append(r.Assertions, fmt.Sprintf("%d:%s:%s", i, st.Transition, c.ID))
			} else {
				r.Status = "failed"
			}
		}
	}
	return r
}

func must(err error) {
	if err != nil {
		panic(err)
	}
}
