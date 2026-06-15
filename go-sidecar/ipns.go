package main

import (
	"encoding/base64"
	"regexp"
)

// IPNS name validation: 12D3KooW... (PeerID) or k... (CIDv1 libp2p-key).
var ipnsNameRE = regexp.MustCompile(`^(12D3KooW[a-zA-Z0-9]{44}|k[a-z2-7]{50,})$`)

// Regex to extract base64-encoded "Extra" field from kubo routing/get JSON.
// Matches: "Extra":"<base64>"
var extraFieldRE = regexp.MustCompile(`"Extra"\s*:\s*"([A-Za-z0-9+/=]+)"`)

// isValidIPNSName checks whether the given string looks like a valid IPNS name.
func isValidIPNSName(name string) bool {
	return ipnsNameRE.MatchString(name)
}

// extractExtraField pulls the base64 "Extra" value from a raw kubo
// routing/get JSON response without parsing the full JSON tree.
func extractExtraField(body []byte) string {
	m := extraFieldRE.FindSubmatch(body)
	if m == nil {
		return ""
	}
	return string(m[1])
}

// buildResponseJSON constructs the pre-serialized routing-get JSON response
// for a marshalled IPNS record: {"Extra":"<base64>","Type":5}
func buildResponseJSON(marshalledRecord []byte) string {
	return `{"Extra":"` + base64.StdEncoding.EncodeToString(marshalledRecord) + `","Type":5}`
}

// parseIPNSRecord extracts the sequence number (field 5) and CID
// from a protobuf-encoded IPNS record.
//
// IPNS record fields:
//   - field 1 (bytes): value (path like /ipfs/<cid>)
//   - field 5 (varint): sequence number
func parseIPNSRecord(data []byte) (sequence int64, cid string) {
	var value string
	pos := 0
	for pos < len(data) {
		// Read field key (varint).
		key, n := readVarint(data[pos:])
		if n == 0 {
			break
		}
		pos += n

		fieldNumber := key >> 3
		wireType := key & 0x07

		switch wireType {
		case 0: // Varint
			val, n := readVarint(data[pos:])
			if n == 0 {
				return sequence, cid
			}
			pos += n
			if fieldNumber == 5 {
				sequence = int64(val)
			}

		case 2: // Length-delimited
			length, n := readVarint(data[pos:])
			if n == 0 {
				return sequence, cid
			}
			pos += n
			if pos+int(length) > len(data) {
				return sequence, cid
			}
			fieldData := data[pos : pos+int(length)]
			pos += int(length)
			if fieldNumber == 1 {
				value = string(fieldData)
			}

		case 1: // 64-bit fixed (double, fixed64, sfixed64)
			pos += 8

		case 5: // 32-bit fixed (float, fixed32, sfixed32)
			pos += 4

		default:
			// Unknown wire type — cannot safely skip.
			return sequence, cid
		}
	}

	// Extract CID from value (e.g., "/ipfs/bafyrei...")
	if len(value) > 6 && value[:6] == "/ipfs/" {
		cid = value[6:]
	}
	return sequence, cid
}

// readVarint reads a protobuf varint from buf and returns (value, bytes_consumed).
// Returns (0, 0) on error.
func readVarint(buf []byte) (uint64, int) {
	var val uint64
	var shift uint
	for i, b := range buf {
		if i >= 10 { // varint too long
			return 0, 0
		}
		val |= uint64(b&0x7F) << shift
		if b&0x80 == 0 {
			return val, i + 1
		}
		shift += 7
	}
	return 0, 0
}
