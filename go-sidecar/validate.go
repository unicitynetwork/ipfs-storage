package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"time"
)

// ValidateChainRequest is the JSON body from the Python pinner.
type ValidateChainRequest struct {
	IPNSName        string `json:"ipns_name"`
	NewCID          string `json:"new_cid"`
	CurrentCID      string `json:"current_cid"`       // empty if first record
	NewSequence     int64  `json:"new_sequence"`
	CurrentSequence int64  `json:"current_sequence"`
	CurrentVersion  int    `json:"current_version"`
}

// ValidateChainResponse is the JSON result sent back to Python.
type ValidateChainResponse struct {
	Valid   bool    `json:"valid"`
	Reason  string  `json:"reason"`
	LastCID *string `json:"last_cid"` // nil → null in JSON
	Version *int    `json:"version"`  // nil → null in JSON
}

type metaField struct {
	Version int
	LastCID *string
}

// ValidateChain handles POST /internal/validate-chain.
//
// Replaces the Python fetch_cid_content + validate_version_chain path to
// eliminate CPython pymalloc fragmentation from HTTP/JSON allocations.
func (h *Handler) ValidateChain(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req ValidateChainRequest
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<16)) // 64 KB max
	if err != nil {
		writeValidateResponse(w, http.StatusBadRequest, false, "bad_request", nil, nil)
		return
	}
	if err := json.Unmarshal(body, &req); err != nil {
		writeValidateResponse(w, http.StatusBadRequest, false, "invalid_json", nil, nil)
		return
	}

	if req.NewCID == "" {
		writeValidateResponse(w, http.StatusBadRequest, false, "missing_new_cid", nil, nil)
		return
	}

	// Defense-in-depth: Python also checks this before calling.
	if !h.cfg.ChainValidationEnabled {
		writeValidateResponse(w, http.StatusOK, true, "validation_disabled", nil, nil)
		return
	}

	// Detect large sequence jump (delta > 5).
	sequenceDelta := req.NewSequence - req.CurrentSequence
	isLargeJump := sequenceDelta > 5
	if isLargeJump {
		log.Printf("validate-chain: large sequence jump for %s: %d -> %d (delta=%d)",
			truncate(req.IPNSName), req.CurrentSequence, req.NewSequence, sequenceDelta)
	}

	// Case 1: First record (bootstrap).
	if req.CurrentCID == "" {
		h.validateBootstrap(w, req)
		return
	}

	// Case 2: Same CID (republish with higher sequence).
	if req.NewCID == req.CurrentCID {
		writeValidateResponse(w, http.StatusOK, true, "republish", &req.CurrentCID, intPtr(0))
		return
	}

	// Case 3 & 4: New CID — fetch content and validate chain.
	content, fetchErr := h.fetchCIDContent(req.NewCID)
	if fetchErr != "" {
		log.Printf("validate-chain: REJECTED: cannot fetch CID %s for %s: %s",
			truncate(req.NewCID), truncate(req.IPNSName), fetchErr)
		h.logChainViolation(req.IPNSName, "fetch_failed", req.CurrentCID, req.NewCID, req.NewSequence, "", "")
		writeValidateResponse(w, http.StatusOK, false, "fetch_failed", nil, nil)
		return
	}

	// Validate _meta structure.
	meta, metaErr := validateMetaField(content, false)
	if metaErr != "" {
		reason := "invalid_meta_" + metaErr
		log.Printf("validate-chain: REJECTED: invalid _meta for %s: %s",
			truncate(req.IPNSName), metaErr)
		h.logChainViolation(req.IPNSName, reason, req.CurrentCID, req.NewCID, req.NewSequence, "", "")
		if isLargeJump {
			log.Printf("validate-chain: SECURITY: large sequence jump with invalid _meta for %s",
				truncate(req.IPNSName))
		}
		writeValidateResponse(w, http.StatusOK, false, reason, nil, nil)
		return
	}

	// Case 3: Large sequence jump — require valid _meta, version >= current.
	if isLargeJump {
		if meta.Version < req.CurrentVersion {
			reason := "large_jump_version_regression"
			log.Printf("validate-chain: REJECTED: large jump version regression for %s: current=%d new=%d",
				truncate(req.IPNSName), req.CurrentVersion, meta.Version)
			h.logChainViolation(req.IPNSName, reason, req.CurrentCID, req.NewCID, req.NewSequence,
				fmt.Sprintf("%d", req.CurrentVersion), fmt.Sprintf("%d", meta.Version))
			writeValidateResponse(w, http.StatusOK, false, reason, nil, nil)
			return
		}
		log.Printf("validate-chain: ACCEPTING large jump for %s: v=%d->%d seq_delta=%d",
			truncate(req.IPNSName), req.CurrentVersion, meta.Version, sequenceDelta)
		writeValidateResponse(w, http.StatusOK, true, "valid_large_jump", meta.LastCID, intPtr(meta.Version))
		return
	}

	// Case 4: Normal new CID — lastCid must equal current CID.
	lastCIDStr := ""
	if meta.LastCID != nil {
		lastCIDStr = *meta.LastCID
	}
	if lastCIDStr != req.CurrentCID {
		log.Printf("validate-chain: CHAIN BREAK for %s: expected lastCid=%s got=%s",
			truncate(req.IPNSName), truncate(req.CurrentCID), truncate(lastCIDStr))
		h.logChainViolation(req.IPNSName, "chain_break", req.CurrentCID, req.NewCID, req.NewSequence,
			req.CurrentCID, lastCIDStr)
		writeValidateResponse(w, http.StatusOK, false, "chain_break", meta.LastCID, nil)
		return
	}

	// Validate version increment: must be exactly current_version + 1.
	expectedVersion := req.CurrentVersion + 1
	if meta.Version != expectedVersion {
		log.Printf("validate-chain: VERSION MISMATCH for %s: expected=%d actual=%d",
			truncate(req.IPNSName), expectedVersion, meta.Version)
		h.logChainViolation(req.IPNSName, "version_mismatch", req.CurrentCID, req.NewCID, req.NewSequence,
			fmt.Sprintf("%d", expectedVersion), fmt.Sprintf("%d", meta.Version))
		writeValidateResponse(w, http.StatusOK, false, "version_mismatch", meta.LastCID, intPtr(meta.Version))
		return
	}

	log.Printf("validate-chain: CHAIN VALID: %s seq=%d v=%d",
		truncate(req.IPNSName), req.NewSequence, meta.Version)
	writeValidateResponse(w, http.StatusOK, true, "valid_chain", meta.LastCID, intPtr(meta.Version))
}

// validateBootstrap handles the first record for an IPNS name.
func (h *Handler) validateBootstrap(w http.ResponseWriter, req ValidateChainRequest) {
	content, fetchErr := h.fetchCIDContent(req.NewCID)
	if fetchErr != "" {
		log.Printf("validate-chain: REJECTED: cannot fetch bootstrap CID %s for %s: %s",
			truncate(req.NewCID), truncate(req.IPNSName), fetchErr)
		h.logChainViolation(req.IPNSName, "fetch_failed_bootstrap", "", req.NewCID, req.NewSequence, "", "")
		writeValidateResponse(w, http.StatusOK, false, "fetch_failed_bootstrap", nil, nil)
		return
	}

	meta, metaErr := validateMetaField(content, true)
	if metaErr != "" {
		reason := "invalid_meta_" + metaErr
		log.Printf("validate-chain: REJECTED: invalid _meta for bootstrap %s: %s",
			truncate(req.IPNSName), metaErr)
		h.logChainViolation(req.IPNSName, reason, "", req.NewCID, req.NewSequence, "", "")
		writeValidateResponse(w, http.StatusOK, false, reason, nil, nil)
		return
	}

	// Bootstrap must NOT have a lastCid.
	if meta.LastCID != nil && *meta.LastCID != "" {
		reason := "invalid_bootstrap_lastcid"
		log.Printf("validate-chain: CHAIN BREAK: bootstrap for %s has unexpected lastCid=%s",
			truncate(req.IPNSName), truncate(*meta.LastCID))
		h.logChainViolation(req.IPNSName, "invalid_bootstrap", "", req.NewCID, req.NewSequence, "", *meta.LastCID)
		writeValidateResponse(w, http.StatusOK, false, reason, nil, nil)
		return
	}

	log.Printf("validate-chain: valid bootstrap for %s v=%d", truncate(req.IPNSName), meta.Version)
	writeValidateResponse(w, http.StatusOK, true, "valid_bootstrap", nil, intPtr(meta.Version))
}

// fetchCIDContent fetches and validates JSON content from kubo /api/v0/cat.
// Returns the parsed map and an empty string on success, or nil and an error reason.
func (h *Handler) fetchCIDContent(cid string) (map[string]interface{}, string) {
	catURL := fmt.Sprintf("%s/api/v0/cat?arg=%s", h.cfg.KuboAPIURL, url.QueryEscape(cid))

	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(h.cfg.CIDFetchTimeout)*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "POST", catURL, nil)
	if err != nil {
		return nil, "request_error"
	}

	resp, err := h.kuboClient.Do(req)
	if err != nil {
		return nil, "timeout_or_network"
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, resp.Body)
		return nil, fmt.Sprintf("status_%d", resp.StatusCode)
	}

	// Use streaming decoder to extract only top-level keys we need (_meta).
	// Wallet states can be 50+ MB; we avoid loading the entire object into memory.
	dec := json.NewDecoder(io.LimitReader(resp.Body, 64<<20))

	// Expect opening '{'.
	tok, err := dec.Token()
	if err != nil {
		return nil, "invalid_json"
	}
	if delim, ok := tok.(json.Delim); !ok || delim != '{' {
		return nil, "invalid_json" // not a JSON object
	}

	content := make(map[string]interface{})
	hasTokens := false
	hasMeta := false
	ipfsInternalKeys := map[string]bool{"Data": true, "Links": true}

	for dec.More() {
		// Read key.
		keyTok, err := dec.Token()
		if err != nil {
			break
		}
		key, ok := keyTok.(string)
		if !ok {
			break
		}

		if key == "_meta" {
			// Parse _meta value fully.
			var metaVal interface{}
			if err := dec.Decode(&metaVal); err != nil {
				return nil, "invalid_json"
			}
			content["_meta"] = metaVal
			hasMeta = true
		} else {
			// Skip the value (don't allocate it).
			var discard json.RawMessage
			if err := dec.Decode(&discard); err != nil {
				break
			}
			// Track whether non-IPFS keys exist (for empty content check).
			if !ipfsInternalKeys[key] {
				hasTokens = true
			}
		}
	}

	if !hasTokens && !hasMeta {
		return nil, "empty_content"
	}

	return content, ""
}

// validateMetaField checks _meta structure strictly.
// Returns the parsed meta and an empty string on success, or nil and an error reason.
func validateMetaField(content map[string]interface{}, isBootstrap bool) (*metaField, string) {
	metaRaw, ok := content["_meta"]
	if !ok {
		return nil, "missing_meta_field"
	}

	metaMap, ok := metaRaw.(map[string]interface{})
	if !ok {
		return nil, "meta_not_dict"
	}

	versionRaw, ok := metaMap["version"]
	if !ok {
		return nil, "missing_meta_version"
	}

	// JSON numbers decode as float64.
	versionFloat, ok := versionRaw.(float64)
	if !ok {
		return nil, "version_not_int"
	}
	version := int(versionFloat)
	if versionFloat != float64(version) {
		return nil, "version_not_int" // fractional
	}
	if version < 1 {
		return nil, "version_less_than_1"
	}

	// Extract lastCid.
	var lastCID *string
	if lastCIDRaw, exists := metaMap["lastCid"]; exists {
		if lastCIDRaw == nil {
			// Explicit null — treated as no lastCid.
			lastCID = nil
		} else if s, ok := lastCIDRaw.(string); ok {
			lastCID = &s
		}
	} else if !isBootstrap {
		// For non-bootstrap records, lastCid field must exist in _meta.
		return nil, "missing_meta_lastcid"
	}

	return &metaField{Version: version, LastCID: lastCID}, ""
}

// logChainViolation writes a forensic record to the chain_validation_log table.
// Runs asynchronously to avoid blocking the validation response on SQLite write contention.
func (h *Handler) logChainViolation(ipnsName, violationType, currentCID, rejectedCID string, rejectedSeq int64, expectedLastCID, actualLastCID string) {
	go func() {
		_, err := h.db.Exec(
			`INSERT INTO chain_validation_log
			 (ipns_name, violation_type, current_cid, rejected_cid,
			  rejected_sequence, expected_lastcid, actual_lastcid)
			 VALUES (?, ?, ?, ?, ?, ?, ?)`,
			ipnsName, violationType,
			nullIfEmpty(currentCID), rejectedCID,
			rejectedSeq,
			nullIfEmpty(expectedLastCID), nullIfEmpty(actualLastCID),
		)
		if err != nil {
			log.Printf("validate-chain: failed to log violation: %v", err)
		}
	}()
}

func writeValidateResponse(w http.ResponseWriter, status int, valid bool, reason string, lastCID *string, version *int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	resp := ValidateChainResponse{
		Valid:   valid,
		Reason:  reason,
		LastCID: lastCID,
		Version: version,
	}
	json.NewEncoder(w).Encode(resp)
}

func intPtr(v int) *int       { return &v }

func nullIfEmpty(s string) interface{} {
	if s == "" {
		return nil
	}
	return s
}
