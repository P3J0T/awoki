package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"path"
	"runtime"
	"strings"
	"time"
)

const maxRequestBytes = 128 * 1024

type request struct {
	Operation string         `json:"operation"`
	Inputs    map[string]any `json:"inputs"`
}

type response struct {
	Status    string `json:"status"`
	Reason    string `json:"reason,omitempty"`
	GoVersion string `json:"go_version"`
	Observed  any    `json:"observed,omitempty"`
}

func emit(status, reason string, observed any) {
	_ = json.NewEncoder(os.Stdout).Encode(response{
		Status: status, Reason: reason, GoVersion: runtime.Version(), Observed: observed,
	})
}

func stringInput(inputs map[string]any, key string) (string, bool) {
	value, ok := inputs[key]
	if !ok {
		return "", false
	}
	text, ok := value.(string)
	return text, ok
}

func intInput(inputs map[string]any, key string) (int, bool) {
	value, ok := inputs[key]
	if !ok {
		return 0, false
	}
	number, ok := value.(float64)
	if !ok || number != float64(int(number)) {
		return 0, false
	}
	return int(number), true
}

func stringSliceInput(inputs map[string]any, key string) ([]string, bool) {
	value, ok := inputs[key]
	if !ok {
		return nil, false
	}
	raw, ok := value.([]any)
	if !ok {
		return nil, false
	}
	out := make([]string, 0, len(raw))
	for _, item := range raw {
		text, ok := item.(string)
		if !ok {
			return nil, false
		}
		out = append(out, text)
	}
	return out, true
}

type fixedTransport struct{}

func (fixedTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return &http.Response{
		StatusCode: http.StatusOK,
		Status:     "200 OK",
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader("")),
		Request:    req,
	}, nil
}

func selectedHeaders(h http.Header) map[string][]string {
	names := []string{
		"Forwarded", "X-Forwarded", "X-Forwarded-For", "X-Forwarded-Host",
		"X-Forwarded-Proto", "X-Forwarded-Port", "X-Forwarded-Uri", "X-Custom",
	}
	out := map[string][]string{}
	for _, name := range names {
		if values, ok := h[http.CanonicalHeaderKey(name)]; ok {
			out[name] = append([]string(nil), values...)
		}
	}
	return out
}

func reverseProxyHeaders() map[string]any {
	req := httptest.NewRequest(http.MethodGet, "http://gateway.example/a", nil)
	req.Header.Set("Forwarded", "for=198.51.100.10;proto=https")
	req.Header.Set("X-Forwarded", "legacy-marker")
	req.Header.Set("X-Forwarded-For", "198.51.100.10")
	req.Header.Set("X-Forwarded-Host", "public.example")
	req.Header.Set("X-Forwarded-Proto", "https")
	req.Header.Set("X-Forwarded-Port", "443")
	req.Header.Set("X-Forwarded-Uri", "/original")
	req.Header.Set("X-Custom", "kept")

	observed := map[string]any{}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			observed["in_at_rewrite"] = selectedHeaders(pr.In.Header)
			observed["out_at_rewrite"] = selectedHeaders(pr.Out.Header)
			pr.Out.URL.Scheme = "http"
			pr.Out.URL.Host = "backend.invalid"
		},
		Transport: fixedTransport{},
	}
	rr := httptest.NewRecorder()
	proxy.ServeHTTP(rr, req)
	observed["response_status"] = rr.Code
	return observed
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Printf("awoki-go-semantics %s\n", runtime.Version())
		return
	}

	decoder := json.NewDecoder(io.LimitReader(os.Stdin, maxRequestBytes+1))
	var req request
	if err := decoder.Decode(&req); err != nil {
		emit("rejected", "invalid request", nil)
		return
	}

	switch req.Operation {
	case "path_join":
		parts, ok := stringSliceInput(req.Inputs, "parts")
		if !ok || len(parts) == 0 || len(parts) > 16 {
			emit("rejected", "path_join requires 1..16 string parts", nil)
			return
		}
		emit("ok", "", map[string]any{"result": path.Join(parts...)})

	case "path_clean":
		value, ok := stringInput(req.Inputs, "path")
		if !ok {
			emit("rejected", "path must be a string", nil)
			return
		}
		emit("ok", "", map[string]any{"result": path.Clean(value)})

	case "parse_duration":
		value, ok := stringInput(req.Inputs, "duration")
		if !ok {
			emit("rejected", "duration must be a string", nil)
			return
		}
		d, err := time.ParseDuration(value)
		observed := map[string]any{
			"input": value, "error": "", "nanoseconds": int64(d), "seconds": d.Seconds(), "string": d.String(),
		}
		if err != nil {
			observed["error"] = err.Error()
		}
		emit("ok", "", observed)

	case "duration_multiply":
		value, ok := stringInput(req.Inputs, "duration")
		if !ok {
			emit("rejected", "duration must be a string", nil)
			return
		}
		unitName, ok := stringInput(req.Inputs, "unit")
		if !ok {
			unitName = "Millisecond"
		}
		units := map[string]time.Duration{
			"Nanosecond": time.Nanosecond, "Microsecond": time.Microsecond,
			"Millisecond": time.Millisecond, "Second": time.Second,
			"Minute": time.Minute, "Hour": time.Hour,
		}
		unit, ok := units[unitName]
		if !ok {
			emit("rejected", "unsupported duration unit", nil)
			return
		}
		d, err := time.ParseDuration(value)
		if err != nil {
			emit("ok", "", map[string]any{"error": err.Error()})
			return
		}
		product := unit * d
		emit("ok", "", map[string]any{
			"duration_input":               value,
			"duration_numeric_nanoseconds": int64(d),
			"unit":                         unitName,
			"unit_numeric_nanoseconds":     int64(unit),
			"product_nanoseconds":          int64(product),
			"product_seconds":              product.Seconds(),
			"product_string":               product.String(),
		})

	case "failed_error_type_assertion":
		var value any = "not an error"
		err, ok := value.(error)
		branch := "neither"
		if ok && err != nil {
			branch = "error"
		} else if err == nil {
			branch = "err_nil"
		} else {
			branch = "type_assert_failed"
		}
		emit("ok", "", map[string]any{"ok": ok, "err_is_nil": err == nil, "branch": branch})

	case "strings_replace":
		value, vok := stringInput(req.Inputs, "value")
		old, ook := stringInput(req.Inputs, "old")
		newValue, nok := stringInput(req.Inputs, "new")
		count, cok := intInput(req.Inputs, "count")
		if !vok || !ook || !nok || !cok {
			emit("rejected", "strings_replace requires string value/old/new and integer count", nil)
			return
		}
		emit("ok", "", map[string]any{"result": strings.Replace(value, old, newValue, count)})

	case "url_parse":
		value, ok := stringInput(req.Inputs, "url")
		if !ok {
			emit("rejected", "url must be a string", nil)
			return
		}
		u, err := url.Parse(value)
		if err != nil {
			emit("ok", "", map[string]any{"error": err.Error()})
			return
		}
		emit("ok", "", map[string]any{
			"scheme": u.Scheme, "host": u.Host, "path": u.Path, "raw_path": u.RawPath,
			"raw_query": u.RawQuery, "fragment": u.Fragment, "string": u.String(),
		})

	case "reverse_proxy_rewrite_headers":
		emit("ok", "", reverseProxyHeaders())

	default:
		emit("rejected", "unsupported operation", nil)
	}
}
