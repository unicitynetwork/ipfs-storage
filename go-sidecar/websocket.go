package main

import (
	"encoding/json"
	"log"
	"net/http"
	"regexp"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

var wsUpgrader = websocket.Upgrader{
	CheckOrigin:     func(r *http.Request) bool { return true }, // CORS handled by nginx
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
}

// SubscriptionManager manages WebSocket subscriptions for IPNS updates.
// Clients connect, subscribe to IPNS names, and receive push notifications.
type SubscriptionManager struct {
	mu   sync.RWMutex
	subs map[string]map[*websocket.Conn]struct{} // ipnsName → set of conns
}

func NewSubscriptionManager() *SubscriptionManager {
	return &SubscriptionManager{
		subs: make(map[string]map[*websocket.Conn]struct{}),
	}
}

// wsMessage is the JSON envelope for client↔server WebSocket messages.
type wsMessage struct {
	Action   string   `json:"action,omitempty"`
	Type     string   `json:"type,omitempty"`
	Names    []string `json:"names,omitempty"`
	Name     string   `json:"name,omitempty"`
	Sequence int64    `json:"sequence,omitempty"`
	CID      string   `json:"cid,omitempty"`
	Message  string   `json:"message,omitempty"`
	// Timestamp is only set on outbound update messages.
	Timestamp string `json:"timestamp,omitempty"`
}

// ipnsNameRE is already defined in ipns.go — reuse for WS validation.
var wsIPNSNameRE = regexp.MustCompile(`^(12D3KooW[a-zA-Z0-9]{44}|k[a-z2-7]{50,})$`)

// HandleWebSocket upgrades an HTTP connection and manages subscriptions.
func (sm *SubscriptionManager) HandleWebSocket(w http.ResponseWriter, r *http.Request) {
	conn, err := wsUpgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("ws: upgrade error: %v", err)
		return
	}
	defer func() {
		sm.removeAll(conn)
		conn.Close()
	}()

	// Set read deadline for idle timeout (client should send ping/subscribe
	// periodically). Reset on every message received.
	conn.SetReadDeadline(time.Now().Add(90 * time.Second))
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(90 * time.Second))
		return nil
	})

	for {
		_, msgBytes, err := conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err,
				websocket.CloseGoingAway,
				websocket.CloseNormalClosure,
				websocket.CloseNoStatusReceived) {
				log.Printf("ws: read error: %v", err)
			}
			break
		}

		conn.SetReadDeadline(time.Now().Add(90 * time.Second))

		var msg wsMessage
		if err := json.Unmarshal(msgBytes, &msg); err != nil {
			sm.sendJSON(conn, wsMessage{Type: "error", Message: "Invalid JSON"})
			continue
		}

		switch msg.Action {
		case "subscribe":
			var valid []string
			for _, name := range msg.Names {
				if wsIPNSNameRE.MatchString(name) {
					sm.addSub(name, conn)
					valid = append(valid, name)
				}
			}
			sm.sendJSON(conn, wsMessage{Type: "subscribed", Names: valid})

		case "unsubscribe":
			for _, name := range msg.Names {
				sm.removeSub(name, conn)
			}
			sm.sendJSON(conn, wsMessage{Type: "unsubscribed", Names: msg.Names})

		case "ping":
			sm.sendJSON(conn, wsMessage{Type: "pong"})

		default:
			sm.sendJSON(conn, wsMessage{Type: "error", Message: "Unknown action"})
		}
	}
}

// Notify pushes an IPNS update to all subscribers of the given name.
func (sm *SubscriptionManager) Notify(ipnsName string, sequence int64, cid string) {
	sm.mu.RLock()
	conns, ok := sm.subs[ipnsName]
	if !ok || len(conns) == 0 {
		sm.mu.RUnlock()
		return
	}
	// Copy the set under read-lock to avoid holding it during writes.
	targets := make([]*websocket.Conn, 0, len(conns))
	for c := range conns {
		targets = append(targets, c)
	}
	sm.mu.RUnlock()

	msg := wsMessage{
		Type:      "update",
		Name:      ipnsName,
		Sequence:  sequence,
		CID:       cid,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}

	for _, c := range targets {
		if err := c.WriteMessage(websocket.TextMessage, data); err != nil {
			// Connection broken — remove it.
			sm.removeAll(c)
			c.Close()
		}
	}
}

// ConnectionCount returns the total number of active WebSocket connections.
func (sm *SubscriptionManager) ConnectionCount() int {
	sm.mu.RLock()
	defer sm.mu.RUnlock()
	seen := make(map[*websocket.Conn]struct{})
	for _, conns := range sm.subs {
		for c := range conns {
			seen[c] = struct{}{}
		}
	}
	return len(seen)
}

func (sm *SubscriptionManager) addSub(ipnsName string, conn *websocket.Conn) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	if sm.subs[ipnsName] == nil {
		sm.subs[ipnsName] = make(map[*websocket.Conn]struct{})
	}
	sm.subs[ipnsName][conn] = struct{}{}
}

func (sm *SubscriptionManager) removeSub(ipnsName string, conn *websocket.Conn) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	if conns, ok := sm.subs[ipnsName]; ok {
		delete(conns, conn)
		if len(conns) == 0 {
			delete(sm.subs, ipnsName)
		}
	}
}

func (sm *SubscriptionManager) removeAll(conn *websocket.Conn) {
	sm.mu.Lock()
	defer sm.mu.Unlock()
	for name, conns := range sm.subs {
		delete(conns, conn)
		if len(conns) == 0 {
			delete(sm.subs, name)
		}
	}
}

func (sm *SubscriptionManager) sendJSON(conn *websocket.Conn, msg wsMessage) {
	conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	data, err := json.Marshal(msg)
	if err != nil {
		return
	}
	conn.WriteMessage(websocket.TextMessage, data)
}
