package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"
	"charm.land/bubbles/v2/key"
	"charm.land/bubbles/v2/list"
	"charm.land/bubbles/v2/spinner"
	"charm.land/bubbles/v2/table"
	"charm.land/bubbles/v2/viewport"
	"charm.land/lipgloss/v2"
)

var (
	// Monochrome with single accent — Apple restraint model
	styleHeader    = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("#fff"))
	styleSubheader = lipgloss.NewStyle().Foreground(lipgloss.Color("#888"))
	styleAccent    = lipgloss.NewStyle().Foreground(lipgloss.Color("#5b8def"))
	styleMuted     = lipgloss.NewStyle().Foreground(lipgloss.Color("#555"))
	styleSuccess   = lipgloss.NewStyle().Foreground(lipgloss.Color("#22c55e"))
	styleDanger    = lipgloss.NewStyle().Foreground(lipgloss.Color("#ef4444"))
	styleBox       = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color("#333")).Padding(0, 1)
	styleBar       = lipgloss.NewStyle().Foreground(lipgloss.Color("#5b8def")).SetString("│")
)

func dataDir() string {
	if d := os.Getenv("SCHOOL_DATA_DIR"); d != "" {
		return d
	}
	return "data"
}

type ActivityEntry struct {
	Type      string    `json:"type"`
	Agent     string    `json:"agent"`
	Bead      string    `json:"bead"`
	Stage     string    `json:"stage"`
	Status    string    `json:"status"`
	Timestamp time.Time `json:"timestamp"`
}

type Trajectory struct {
	File      string         `json:"-"`
	Timestamp time.Time      `json:"-"`
	Domain    string         `json:"-"`
	Role      string         `json:"-"`
	Content   map[string]any `json:"-"`
}

type model struct {
	currentView int
	width       int
	height      int

	scores     map[string]map[string]float64
	activities []ActivityEntry
	trajs      []Trajectory
	trajList   list.Model
	viewport   viewport.Model
	spinner    spinner.Model
	keys       keyMap
	loading    bool
	err        error

	// Computed tables
	rosterTable  table.Model
	boardColumns map[string][]ActivityEntry
}

type keyMap struct {
	Tab  key.Binding
	Quit key.Binding
}

var defaultKeys = keyMap{
	Tab: key.NewBinding(
		key.WithKeys("tab"),
		key.WithHelp("tab", "next"),
	),
	Quit: key.NewBinding(
		key.WithKeys("q", "ctrl+c"),
		key.WithHelp("q", "quit"),
	),
}

func initialModel() model {
	return model{
		currentView: 0,
		scores:      make(map[string]map[string]float64),
		keys:        defaultKeys,
		spinner:     spinner.New(spinner.WithSpinner(spinner.Dot)),
		loading:     true,
	}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(
		m.spinner.Tick,
		loadScores(),
		loadActivities(),
		loadTrajectories(),
	)
}

// ── Data loaders ────────────────────────────────────────────────────────────

func loadScores() tea.Cmd {
	return func() tea.Msg {
		path := filepath.Join(dataDir(), "scores.json")
		f, err := os.Open(path)
		if err != nil {
			return errMsg{err}
		}
		defer f.Close()

		var raw map[string]map[string]any
		if err := json.NewDecoder(f).Decode(&raw); err != nil {
			return errMsg{err}
		}

		scores := make(map[string]map[string]float64)
		for persona, domainMap := range raw {
			scores[persona] = make(map[string]float64)
			for domain, v := range domainMap {
				if f, ok := v.(float64); ok {
					scores[persona][domain] = f
				}
			}
		}
		return scoresMsg(scores)
	}
}

func loadActivities() tea.Cmd {
	return func() tea.Msg {
		path := filepath.Join(dataDir(), "activity_log.json")
		f, err := os.Open(path)
		if err != nil {
			return errMsg{err}
		}
		defer f.Close()

		var raw struct {
			Entries []ActivityEntry `json:"entries"`
		}
		if err := json.NewDecoder(f).Decode(&raw); err != nil {
			f.Seek(0, 0)
			var entries []ActivityEntry
			if err := json.NewDecoder(f).Decode(&entries); err != nil {
				return errMsg{err}
			}
			return activitiesMsg(entries)
		}
		return activitiesMsg(raw.Entries)
	}
}

func loadTrajectories() tea.Cmd {
	return func() tea.Msg {
		dir := filepath.Join(dataDir(), "trajectories")
		entries, err := os.ReadDir(dir)
		if err != nil {
			return errMsg{err}
		}

		var trajs []Trajectory
		for _, e := range entries {
			if !strings.HasSuffix(e.Name(), ".json") {
				continue
			}
			path := filepath.Join(dir, e.Name())
			f, err := os.Open(path)
			if err != nil {
				continue
			}
			var content map[string]any
			json.NewDecoder(f).Decode(&content)
			f.Close()

			parts := strings.Split(strings.TrimSuffix(e.Name(), ".json"), "--")
			t := Trajectory{File: e.Name(), Content: content}
			if len(parts) >= 3 {
				t.Timestamp, _ = time.Parse("20060102_150405", parts[0][:15])
				t.Domain = parts[1]
				t.Role = parts[2]
			}
			trajs = append(trajs, t)
		}

		sort.Slice(trajs, func(i, j int) bool {
			return trajs[i].Timestamp.After(trajs[j].Timestamp)
		})
		return trajectoriesMsg(trajs)
	}
}

// Messages
type scoresMsg map[string]map[string]float64
type activitiesMsg []ActivityEntry
type trajectoriesMsg []Trajectory
type errMsg struct{ err error }

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		if m.viewport.Width() == 0 {
			m.viewport = viewport.New(viewport.WithWidth(msg.Width), viewport.WithHeight(msg.Height-6))
		}

	case tea.KeyMsg:
		switch {
		case key.Matches(msg, m.keys.Quit):
			return m, tea.Quit
		case key.Matches(msg, m.keys.Tab):
			m.currentView = (m.currentView + 1) % 4
			return m, nil
		}

	case spinner.TickMsg:
		var cmd tea.Cmd
		m.spinner, cmd = m.spinner.Update(msg)
		return m, cmd

	case scoresMsg:
		m.scores = map[string]map[string]float64(msg)
		m.buildRosterTable()
		m.loading = false

	case activitiesMsg:
		m.activities = []ActivityEntry(msg)
		m.buildBoardColumns()

	case trajectoriesMsg:
		m.trajs = []Trajectory(msg)
		items := make([]list.Item, len(m.trajs))
		for i := range m.trajs {
			items[i] = trajItem{t: m.trajs[i]}
		}
		m.trajList = list.New(items, list.NewDefaultDelegate(), 0, 0)
		m.trajList.Title = "Trajectories"
		m.trajList.SetShowHelp(false)

	case errMsg:
		m.err = msg.err
		m.loading = false
	}

	if m.currentView == 2 {
		var cmd tea.Cmd
		m.trajList, cmd = m.trajList.Update(msg)
		return m, cmd
	}

	return m, nil
}

func (m model) View() tea.View {
	if m.err != nil {
		return tea.NewView(fmt.Sprintf("Error: %v\n\nq to quit\n", m.err))
	}

	if m.loading {
		return tea.NewView(fmt.Sprintf("\n  %s Loading school data...\n", m.spinner.View()))
	}

	var b strings.Builder
	b.WriteString(m.renderHeader())
	b.WriteString("\n")

	switch m.currentView {
	case 0:
		b.WriteString(m.renderBoard())
	case 1:
		b.WriteString(m.renderRoster())
	case 2:
		b.WriteString(m.renderTrajectories())
	case 3:
		b.WriteString(m.renderActivity())
	}

	b.WriteString("\n" + styleMuted.Render("  [tab] next view  ·  [q] quit"))
	b.WriteString("\n")

	return tea.NewView(b.String())
}

func (m model) renderHeader() string {
	title := styleHeader.Render("Agent School")
	sub := styleSubheader.Render("v1.0")
	return fmt.Sprintf("  %s %s\n  %s\n", title, sub, strings.Repeat("─", m.width-4))
}

func (m model) renderBoard() string {
	cols := []struct {
		key   string
		label string
		color lipgloss.Style
	}{
		{"in_progress", "IN PROGRESS", styleAccent},
		{"review", "IN REVIEW", styleSubheader},
		{"done", "DONE", styleSuccess},
		{"boot", "BOOTING", styleMuted},
	}

	var topRows []string
	for _, col := range cols {
		entries := m.boardColumns[col.key]
		count := len(entries)

		header := col.color.Bold(true).Render(fmt.Sprintf("%s (%d)", col.label, count))

		var body []string
		for _, e := range entries[:min(3, len(entries))] {
			ts := e.Timestamp.Format("01/02 15:04")
			body = append(body, fmt.Sprintf("│ %s %s", ts, e.Agent))
		}
		if len(entries) > 3 {
			body = append(body, fmt.Sprintf("│ ...+%d more", len(entries)-3))
		}
		if len(entries) == 0 {
			body = append(body, "│ (empty)")
		}

		content := append([]string{header}, body...)
		topRows = append(topRows, styleBox.Render(strings.Join(content, "\n")))
	}

	return lipgloss.JoinHorizontal(lipgloss.Top, topRows...) + "\n"
}

func (m model) renderRoster() string {
	rows := []table.Row{}

	type personaRow struct {
		name    string
		ema     float64
		domains []string
	}
	var prow []personaRow
	for name, domains := range m.scores {
		bestEMA := 0.0
		for _, v := range domains {
			if v > bestEMA {
				bestEMA = v
			}
		}
		prow = append(prow, personaRow{name: name, ema: bestEMA, domains: sortedKeys(domains)})
	}
	sort.Slice(prow, func(i, j int) bool {
		return prow[i].ema > prow[j].ema
	})

	for _, r := range prow {
		domainsStr := strings.Join(r.domains, ", ")
		if len(r.domains) == 0 {
			domainsStr = "—"
		}
		rows = append(rows, table.Row{
			r.name,
			fmt.Sprintf("%.1f", r.ema),
			domainsStr,
		})
	}

	t := table.New(
		table.WithRows(rows),
		table.WithColumns([]table.Column{
			{Title: "PERSONA", Width: 14},
			{Title: "EMA", Width: 6},
			{Title: "DOMAINS", Width: 30},
		}),
		table.WithFocused(false),
		table.WithHeight(min(len(rows)+2, m.height-6)),
	)

	return t.View() + "\n"
}

func (m model) renderTrajectories() string {
	m.trajList.SetSize(m.width, m.height-6)
	return m.trajList.View()
}

func (m model) renderActivity() string {
	var lines []string
	for i := len(m.activities) - 1; i >= 0 && i >= len(m.activities)-20; i-- {
		a := m.activities[i]
		ts := a.Timestamp.Format("15:04:05")
		lines = append(lines, fmt.Sprintf("%s  %-20s %s", ts, a.Agent, a.Stage))
	}
	m.viewport.SetContent(strings.Join(lines, "\n"))
	return m.viewport.View()
}

func (m model) buildRosterTable() {
	// Triggered by scoresMsg
}

func (m model) buildBoardColumns() {
	m.boardColumns = map[string][]ActivityEntry{}
	for _, a := range m.activities {
		m.boardColumns[a.Status] = append(m.boardColumns[a.Status], a)
	}
}

func sortedKeys(m map[string]float64) []string {
	var keys []string
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// trajItem adapts a Trajectory for the bubbles/list component.
type trajItem struct {
	t Trajectory
}

func (i trajItem) Title() string {
	return fmt.Sprintf("%s  %s  %s", i.t.Timestamp.Format("01/02 15:04"), i.t.Domain, i.t.Role)
}

func (i trajItem) Description() string {
	if v, ok := i.t.Content["status"]; ok {
		return fmt.Sprintf("status=%v", v)
	}
	return i.t.File
}

func (i trajItem) FilterValue() string {
	return fmt.Sprintf("%s %s %s", i.t.File, i.t.Domain, i.t.Role)
}

func main() {
	p := tea.NewProgram(initialModel())
	if _, err := p.Run(); err != nil {
		fmt.Printf("Error: %v\n", err)
		os.Exit(1)
	}
}
