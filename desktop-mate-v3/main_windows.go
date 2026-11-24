//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"image"
	"image/draw"
	"math/rand"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	widthBase        = 360
	heightBase       = 400
	wmDestroy        = 0x0002
	wmTimer          = 0x0113
	wmLButtonDown    = 0x0201
	wmLButtonUp      = 0x0202
	wmMouseMove      = 0x0200
	wmContextMenu    = 0x007B
	wmClose          = 0x0010
	wmNull           = 0x0000
	wsPopup          = 0x80000000
	exLayered        = 0x00080000
	exToolWindow     = 0x00000080
	exTopmost        = 0x00000008
	swShowNoActivate = 4
	ulwAlpha         = 0x00000002
	acSrcAlpha       = 0x01
	mfString         = 0x0000
	mfPopup          = 0x0010
	mfSeparator      = 0x0800
	mfChecked        = 0x0008
	tpmRightButton   = 0x0002
	tpmReturnCmd     = 0x0100
	swpNoSize        = 0x0001
	swpNoZOrder      = 0x0004
	swpNoActivate    = 0x0010
	swpShowWindow    = 0x0040
	hwndTopmost      = ^uintptr(0)
	hwndNoTopmost    = ^uintptr(1)
)

var (
	user32   = syscall.NewLazyDLL("user32.dll")
	gdi32    = syscall.NewLazyDLL("gdi32.dll")
	kernel32 = syscall.NewLazyDLL("kernel32.dll")
	advapi32 = syscall.NewLazyDLL("advapi32.dll")

	registerClass        = user32.NewProc("RegisterClassExW")
	createWindow         = user32.NewProc("CreateWindowExW")
	defWindowProc        = user32.NewProc("DefWindowProcW")
	showWindow           = user32.NewProc("ShowWindow")
	updateWindow         = user32.NewProc("UpdateWindow")
	getMessage           = user32.NewProc("GetMessageW")
	translateMessage     = user32.NewProc("TranslateMessage")
	dispatchMessage      = user32.NewProc("DispatchMessageW")
	postQuit             = user32.NewProc("PostQuitMessage")
	setTimer             = user32.NewProc("SetTimer")
	killTimer            = user32.NewProc("KillTimer")
	getDC                = user32.NewProc("GetDC")
	releaseDC            = user32.NewProc("ReleaseDC")
	updateLayered        = user32.NewProc("UpdateLayeredWindow")
	getSystemMetrics     = user32.NewProc("GetSystemMetrics")
	systemParametersInfo = user32.NewProc("SystemParametersInfoW")
	getCursorPos         = user32.NewProc("GetCursorPos")
	setCapture           = user32.NewProc("SetCapture")
	releaseCapture       = user32.NewProc("ReleaseCapture")
	setWindowPos         = user32.NewProc("SetWindowPos")
	createPopupMenu      = user32.NewProc("CreatePopupMenu")
	appendMenu           = user32.NewProc("AppendMenuW")
	trackPopupMenu       = user32.NewProc("TrackPopupMenu")
	destroyMenu          = user32.NewProc("DestroyMenu")
	setForegroundWindow  = user32.NewProc("SetForegroundWindow")
	postMessage          = user32.NewProc("PostMessageW")
	messageBox           = user32.NewProc("MessageBoxW")
	destroyWindow        = user32.NewProc("DestroyWindow")
	createCompatibleDC   = gdi32.NewProc("CreateCompatibleDC")
	deleteDC             = gdi32.NewProc("DeleteDC")
	createDIBSection     = gdi32.NewProc("CreateDIBSection")
	selectObject         = gdi32.NewProc("SelectObject")
	deleteObject         = gdi32.NewProc("DeleteObject")
	getModuleHandle      = kernel32.NewProc("GetModuleHandleW")
	setDPIAware          = user32.NewProc("SetProcessDPIAware")
	regOpenKey           = advapi32.NewProc("RegOpenKeyExW")
	regSetValue          = advapi32.NewProc("RegSetValueExW")
	regDeleteValue       = advapi32.NewProc("RegDeleteValueW")
	regCloseKey          = advapi32.NewProc("RegCloseKey")
)

type point struct{ X, Y int32 }
type size struct{ W, H int32 }
type rect struct{ Left, Top, Right, Bottom int32 }
type wndClassEx struct {
	Size, Style                                                        uint32
	WndProc                                                            uintptr
	ClassExtra, WindowExtra                                            int32
	Instance, Icon, Cursor, Background, MenuName, ClassName, IconSmall uintptr
}
type winMessage struct {
	Hwnd           uintptr
	Message        uint32
	_              uint32
	WParam, LParam uintptr
	Time           uint32
	Pt             point
	_              uint32
}
type bitmapInfoHeader struct {
	Size                         uint32
	Width, Height                int32
	Planes, BitCount             uint16
	Compression, SizeImage       uint32
	XPelsPerMeter, YPelsPerMeter int32
	ClrUsed, ClrImportant        uint32
}
type bitmapInfo struct {
	Header bitmapInfoHeader
	Colors [1]uint32
}
type blendFunction struct{ Op, Flags, Alpha, Format byte }

type dib struct {
	screenDC, memDC, bitmap, oldBitmap uintptr
	bits                               unsafe.Pointer
	w, h                               int
}

func (d *dib) close() {
	if d.memDC != 0 {
		if d.oldBitmap != 0 {
			selectObject.Call(d.memDC, d.oldBitmap)
		}
		if d.bitmap != 0 {
			deleteObject.Call(d.bitmap)
		}
		deleteDC.Call(d.memDC)
	}
	if d.screenDC != 0 {
		releaseDC.Call(0, d.screenDC)
	}
	*d = dib{}
}
func (d *dib) init(w, h int) error {
	d.close()
	dc, _, _ := getDC.Call(0)
	if dc == 0 {
		return fmt.Errorf("GetDC failed")
	}
	mem, _, _ := createCompatibleDC.Call(dc)
	if mem == 0 {
		releaseDC.Call(0, dc)
		return fmt.Errorf("CreateCompatibleDC failed")
	}
	bmi := bitmapInfo{Header: bitmapInfoHeader{Size: uint32(unsafe.Sizeof(bitmapInfoHeader{})), Width: int32(w), Height: -int32(h), Planes: 1, BitCount: 32}}
	var bits unsafe.Pointer
	bmp, _, _ := createDIBSection.Call(mem, uintptr(unsafe.Pointer(&bmi)), 0, uintptr(unsafe.Pointer(&bits)), 0, 0)
	if bmp == 0 || bits == nil {
		deleteDC.Call(mem)
		releaseDC.Call(0, dc)
		return fmt.Errorf("CreateDIBSection failed")
	}
	old, _, _ := selectObject.Call(mem, bmp)
	d.screenDC = dc
	d.memDC = mem
	d.bitmap = bmp
	d.oldBitmap = old
	d.bits = bits
	d.w = w
	d.h = h
	return nil
}

func (d *dib) copyRGBA(src *image.RGBA) {
	raw := unsafe.Slice((*byte)(d.bits), 4*d.w*d.h)
	for y := 0; y < d.h; y++ {
		line := src.Pix[y*src.Stride:]
		for x := 0; x < d.w; x++ {
			i := x * 4
			j := (y*d.w + x) * 4
			raw[j] = line[i+2]
			raw[j+1] = line[i+1]
			raw[j+2] = line[i]
			raw[j+3] = line[i+3]
		}
	}
}

func textPtr(s string) uintptr { return uintptr(unsafe.Pointer(syscall.StringToUTF16Ptr(s))) }

// Pure-Go quality bilinear scaling on premultiplied RGBA. Cached when settings change.
func resizeRGBA(src image.Image, w, h int) *image.RGBA {
	out := image.NewRGBA(image.Rect(0, 0, w, h))
	b := src.Bounds()
	sw, sh := b.Dx(), b.Dy()
	if w < 1 || h < 1 || sw < 1 || sh < 1 {
		return out
	}
	if sw == w && sh == h {
		draw.Draw(out, out.Bounds(), src, b.Min, draw.Src)
		return out
	}
	// image.RGBA.At returns unpremultiplied color when using RGBA(), suitable for mixing.
	for y := 0; y < h; y++ {
		fy := (float64(y)+.5)*float64(sh)/float64(h) - .5
		y0 := int(fy)
		wy := fy - float64(y0)
		if y0 < 0 {
			y0 = 0
			wy = 0
		}
		if y0 >= sh-1 {
			y0 = sh - 1
			wy = 0
		}
		y1 := y0 + 1
		if y1 >= sh {
			y1 = y0
		}
		for x := 0; x < w; x++ {
			fx := (float64(x)+.5)*float64(sw)/float64(w) - .5
			x0 := int(fx)
			wx := fx - float64(x0)
			if x0 < 0 {
				x0 = 0
				wx = 0
			}
			if x0 >= sw-1 {
				x0 = sw - 1
				wx = 0
			}
			x1 := x0 + 1
			if x1 >= sw {
				x1 = x0
			}
			r00, g00, b00, a00 := src.At(b.Min.X+x0, b.Min.Y+y0).RGBA()
			r10, g10, b10, a10 := src.At(b.Min.X+x1, b.Min.Y+y0).RGBA()
			r01, g01, b01, a01 := src.At(b.Min.X+x0, b.Min.Y+y1).RGBA()
			r11, g11, b11, a11 := src.At(b.Min.X+x1, b.Min.Y+y1).RGBA()
			i := y*out.Stride + x*4
			vals := [4][4]uint32{{r00, g00, b00, a00}, {r10, g10, b10, a10}, {r01, g01, b01, a01}, {r11, g11, b11, a11}}
			for c := 0; c < 4; c++ {
				v0 := float64(vals[0][c])*(1-wx) + float64(vals[1][c])*wx
				v1 := float64(vals[2][c])*(1-wx) + float64(vals[3][c])*wx
				v := (v0*(1-wy) + v1*wy) / 257.0
				if v < 0 {
					v = 0
				}
				if v > 255 {
					v = 255
				}
				out.Pix[i+c] = byte(v + .5)
			}
		}
	}
	return out
}

type mate struct {
	hwnd         uintptr
	s            Settings
	x, y, w, h   int
	surface      dib
	clips        map[Behavior]clip
	packError    string
	sprite       image.Image
	scaledSprite *image.RGBA
	spriteRect   image.Rectangle
	player       playback
	nextAction   time.Time
	previewAuto  bool
	rng          *rand.Rand
	bubble       *image.RGBA
	labels       map[string]*image.RGBA
	digits       map[rune]*image.RGBA
	flashUntil   time.Time
	flashLabel   string
	dragging     bool
	moved        bool
	dragStart    point
	windowStart  point
	configFile   string
	startup      bool
}

var app *mate

func (a *mate) loadImages() error {
	load := func(name string) (image.Image, error) { return loadResource(name) }
	im, e := load("assets/ui/bubble.png")
	if e != nil {
		return e
	}
	a.bubble = resizeRGBA(im, 342, 114)
	a.labels = make(map[string]*image.RGBA)
	for _, k := range []string{"work", "before", "lunch", "done", "weekend", "lunch_soon", "soon", "afternoon",
		"sipclose", "savor",
		"click1", "click2", "click3", "click4", "click5"} {
		im, e := load("assets/ui/label_" + k + ".png")
		if e != nil {
			return e
		}
		a.labels[k] = resizeRGBA(im, im.Bounds().Dx(), im.Bounds().Dy())
	}
	a.digits = make(map[rune]*image.RGBA)
	for _, r := range "0123456789:" {
		n := string(r)
		if r == ':' {
			n = "colon"
		}
		im, e := load("assets/ui/digit_" + n + ".png")
		if e != nil {
			return e
		}
		a.digits[r] = resizeRGBA(im, im.Bounds().Dx(), im.Bounds().Dy())
	}
	return nil
}

func (a *mate) loadAnimations() {
	exe, err := os.Executable()
	if err != nil {
		a.packError = err.Error()
		return
	}
	dir := filepath.Join(filepath.Dir(exe), "assets", "actions")
	if _, err := os.Stat(filepath.Join(dir, "pack.json")); os.IsNotExist(err) {
		a.packError = "실행 파일 옆 assets/actions/pack.json에 준비한 동작 팩을 넣고 다시 실행하세요."
		return
	}
	a.clips, err = readPack(os.DirFS(dir))
	if err != nil {
		a.packError = err.Error()
	}
}

func (a *mate) loadSettings() {
	a.s = defaultSettings()
	dir, err := os.UserConfigDir()
	if err != nil {
		return
	}
	a.configFile = filepath.Join(dir, "KurimanjuMateV31", "settings.json")
	body, err := os.ReadFile(a.configFile)
	if os.IsNotExist(err) {
		body, err = os.ReadFile(filepath.Join(dir, "KurimanjuMateV3", "settings.json"))
	}
	if os.IsNotExist(err) {
		body, err = os.ReadFile(filepath.Join(dir, "KurimanjuMateNext", "settings.json"))
	}
	if os.IsNotExist(err) {
		body, err = os.ReadFile(filepath.Join(dir, "KurimanjuMate", "settings.json"))
	}
	if err == nil {
		var m map[string]json.RawMessage
		if json.Unmarshal(body, &m) == nil {
			json.Unmarshal(body, &a.s)
		}
	}
	a.s = validSettings(a.s)
}
func (a *mate) saveSettings() {
	if a.configFile == "" {
		return
	}
	dir := filepath.Dir(a.configFile)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return
	}
	raw, err := json.MarshalIndent(a.s, "", "  ")
	if err != nil {
		return
	}
	os.WriteFile(a.configFile, raw, 0600)
}
func (a *mate) initSize() error {
	a.w = widthBase * a.s.Scale / 100
	a.h = heightBase * a.s.Scale / 100
	return a.surface.init(a.w, a.h)
}
func (a *mate) defaultPosition() {
	if a.s.Placed {
		a.x = a.s.X
		a.y = a.s.Y
		return
	}
	var rc rect
	ok, _, _ := systemParametersInfo.Call(0x0030, 0, uintptr(unsafe.Pointer(&rc)), 0)
	if ok != 0 {
		a.x = int(rc.Right) - a.w - 15
		a.y = int(rc.Bottom) - a.h - 12
	} else {
		sx, _, _ := getSystemMetrics.Call(0)
		sy, _, _ := getSystemMetrics.Call(1)
		a.x = int(sx) - a.w - 15
		a.y = int(sy) - a.h - 55
	}
	if a.x < 0 {
		a.x = 0
	}
	if a.y < 0 {
		a.y = 0
	}
}
func (a *mate) render() {
	if a.hwnd == 0 || a.surface.bits == nil {
		return
	}
	canvas := image.NewRGBA(image.Rect(0, 0, widthBase, heightBase))
	draw.Draw(canvas, image.Rect(9, 4, 351, 118), a.bubble, image.Point{}, draw.Over)
	now := time.Now()
	status := statusAt(now, a.s)
	label := status.Label
	if now.Before(a.player.until) {
		switch a.player.action {
		case Can, Lemon:
			label = "sipclose"
		case Soup:
			label = "savor"
		case Work:
			label = "work"
		}
	}
	if now.Before(a.flashUntil) {
		label = a.flashLabel
	}
	if a.labels[label] == nil {
		label = "work"
	}
	lbl := a.labels[label]
	// When a motivational speech bubble is visible, keep clock display below it when timed.
	lblY := 16
	if !status.Timed {
		lblY = 36
	}
	draw.Draw(canvas, image.Rect((widthBase-lbl.Bounds().Dx())/2, lblY, (widthBase+lbl.Bounds().Dx())/2, lblY+lbl.Bounds().Dy()), lbl, image.Point{}, draw.Over)
	if status.Timed {
		countdown := countdownFormat(status.Remaining)
		total := 0
		for _, c := range countdown {
			total += a.digits[c].Bounds().Dx()
		}
		x := (widthBase - total) / 2
		for _, c := range countdown {
			img := a.digits[c]
			draw.Draw(canvas, image.Rect(x, 57, x+img.Bounds().Dx(), 57+img.Bounds().Dy()), img, image.Point{}, draw.Over)
			x += img.Bounds().Dx()
		}
	}
	if active, ok := a.clips[a.player.action]; ok {
		sprite, _ := active.frameAt(now.Sub(a.player.started))
		if a.sprite != sprite {
			src := sprite.Bounds()
			w, h := src.Dx(), src.Dy()
			if w*270 > h*320 {
				w, h = 320, max(1, h*320/w)
			} else {
				w, h = max(1, w*270/h), 270
			}
			x, y := (widthBase-w)/2, 390-h
			a.sprite, a.scaledSprite = sprite, resizeRGBA(sprite, w, h)
			a.spriteRect = image.Rect(x, y, x+w, y+h)
		}
		draw.Draw(canvas, a.spriteRect, a.scaledSprite, image.Point{}, draw.Over)
	}
	// Scaled frame drawn into transparent premultiplied BGRA DIB.
	if a.s.Scale != 100 {
		canvas = resizeRGBA(canvas, a.w, a.h)
	}
	a.surface.copyRGBA(canvas)
	pt := point{int32(a.x), int32(a.y)}
	sz := size{int32(a.w), int32(a.h)}
	source := point{0, 0}
	blend := blendFunction{Op: 0, Flags: 0, Alpha: 255, Format: acSrcAlpha}
	updateLayered.Call(a.hwnd, a.surface.screenDC, uintptr(unsafe.Pointer(&pt)), uintptr(unsafe.Pointer(&sz)),
		a.surface.memDC, uintptr(unsafe.Pointer(&source)), 0, uintptr(unsafe.Pointer(&blend)), ulwAlpha)
}

func (a *mate) rest() time.Duration {
	if a.previewAuto {
		return previewInterval
	}
	return restFor(a.s.Frequency, a.rng.Intn(10000))
}

func (a *mate) startAction(kind Behavior, now time.Time) {
	duration := time.Duration(0)
	if kind != Idle {
		active, ok := a.clips[kind]
		if !ok {
			return
		}
		duration = active.duration()
	}
	a.player.start(kind, now, duration)
	a.nextAction = a.player.until.Add(a.rest())
}

func (a *mate) requestAction(kind Behavior, now time.Time) {
	if _, ok := a.clips[kind]; !ok {
		return
	}
	a.player.request(kind)
	if next := a.player.take(now); next != "" {
		a.startAction(next, now)
		a.render()
		a.armTimer(time.Now())
	}
}

func (a *mate) armTimer(now time.Time) {
	delay := 100 * time.Millisecond
	if active, ok := a.clips[a.player.action]; ok {
		_, next := active.frameAt(now.Sub(a.player.started))
		delay = min(delay, next)
	}
	ms := max(10, (delay+time.Millisecond-1)/time.Millisecond)
	setTimer.Call(a.hwnd, 1, uintptr(ms), 0)
}

func (a *mate) frameTick() {
	now := time.Now()
	wasSpecial := a.player.action != Idle
	a.player.advance(now)
	if wasSpecial && a.player.action == Idle {
		a.nextAction = now.Add(a.rest())
	}
	if next := a.player.take(now); next != "" {
		a.startAction(next, now)
	} else if !now.Before(a.nextAction) {
		status := automaticStatus(now, a.nextAction, a.player, a.s, a.clips, a.previewAuto, a.rng.Intn(10000))
		if status.action != Idle {
			a.startAction(status.action, now)
		} else if a.player.action == Idle && !now.Before(a.player.ready) {
			a.nextAction = now.Add(a.rest())
		}
	}
	a.render()
	a.armTimer(time.Now())
}
func (a *mate) setSize(n int) {
	if a.s.Scale == n {
		return
	}
	a.s.Scale = n
	if err := a.initSize(); err != nil {
		a.alert("화면 크기 변경 오류", err.Error())
		return
	}
	setWindowPos.Call(a.hwnd, 0, uintptr(a.x), uintptr(a.y), uintptr(a.w), uintptr(a.h), swpNoZOrder|swpNoActivate)
	a.saveSettings()
	a.render()
}
func (a *mate) setTop(v bool) {
	a.s.AlwaysOnTop = v
	z := hwndNoTopmost
	if v {
		z = hwndTopmost
	}
	setWindowPos.Call(a.hwnd, z, 0, 0, 0, 0, swpNoSize|swpNoActivate|0x0002)
	a.saveSettings()
}
func (a *mate) resetPosition() {
	a.s.Placed = false
	a.defaultPosition()
	setWindowPos.Call(a.hwnd, 0, uintptr(a.x), uintptr(a.y), 0, 0, swpNoSize|swpNoZOrder|swpNoActivate)
	a.s.X = a.x
	a.s.Y = a.y
	a.s.Placed = true
	a.saveSettings()
	a.render()
}
func (a *mate) alert(title, desc string) {
	messageBox.Call(a.hwnd, textPtr(desc), textPtr(title), 0x40)
}
func (a *mate) toggleStartup() {
	desired := !a.startup
	if err := setAutoRun(desired); err != nil {
		a.alert("자동실행 설정 실패", err.Error())
		return
	}
	a.startup = desired
}
func setAutoRun(enabled bool) error {
	const hkcu = uintptr(0x80000001)
	var h uintptr
	subKey := syscall.StringToUTF16Ptr(`Software\Microsoft\Windows\CurrentVersion\Run`)
	ret, _, _ := regOpenKey.Call(hkcu, uintptr(unsafe.Pointer(subKey)), 0, 0x0002, uintptr(unsafe.Pointer(&h)))
	if ret != 0 {
		return fmt.Errorf("레지스트리에 접근하지 못했습니다 (코드 %d)", ret)
	}
	defer regCloseKey.Call(h)
	name := syscall.StringToUTF16Ptr("KurimanjuMateV31")
	if !enabled {
		ret, _, _ := regDeleteValue.Call(h, uintptr(unsafe.Pointer(name)))
		if ret != 0 && ret != 2 {
			return fmt.Errorf("자동실행 해제 실패 (코드 %d)", ret)
		}
		return nil
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	value := syscall.StringToUTF16("\"" + exe + "\"")
	ret, _, _ = regSetValue.Call(h, uintptr(unsafe.Pointer(name)), 0, 1, uintptr(unsafe.Pointer(&value[0])), uintptr(len(value)*2))
	if ret != 0 {
		return fmt.Errorf("자동실행 등록 실패 (코드 %d)", ret)
	}
	return nil
}
func isAutoRun() bool {
	// State check is based on registration existence, not inferred from preferences.
	var h uintptr
	const hkcu = uintptr(0x80000001)
	sub := syscall.StringToUTF16Ptr(`Software\Microsoft\Windows\CurrentVersion\Run`)
	ret, _, _ := regOpenKey.Call(hkcu, uintptr(unsafe.Pointer(sub)), 0, 0x0001, uintptr(unsafe.Pointer(&h)))
	if ret != 0 {
		return false
	}
	defer regCloseKey.Call(h)
	query := advapi32.NewProc("RegQueryValueExW")
	name := syscall.StringToUTF16Ptr("KurimanjuMateV31")
	var needed uint32
	r, _, _ := query.Call(h, uintptr(unsafe.Pointer(name)), 0, 0, 0, uintptr(unsafe.Pointer(&needed)))
	return r == 0
}

func menuAppend(menu uintptr, id uintptr, title string, checked bool) {
	flag := uintptr(mfString)
	if checked {
		flag |= mfChecked
	}
	appendMenu.Call(menu, flag, id, textPtr(title))
}
func menuSubmenu(parent uintptr, title string, items []struct {
	id      uintptr
	label   string
	checked bool
}) {
	sub, _, _ := createPopupMenu.Call()
	for _, it := range items {
		menuAppend(sub, it.id, it.label, it.checked)
	}
	appendMenu.Call(parent, mfPopup, sub, textPtr(title))
}
func (a *mate) contextMenu() {
	m, _, _ := createPopupMenu.Call()
	if m == 0 {
		return
	}
	menuAppend(m, 615, "자동 동작 진단", false)
	if len(a.clips) > 0 {
		preview, _, _ := createPopupMenu.Call()
		if preview != 0 {
			for i, action := range actions {
				if _, ok := a.clips[action.kind]; ok {
					menuAppend(preview, uintptr(801+i), action.name, false)
				}
			}
			if _, ok := a.clips[Work]; ok {
				menuAppend(preview, 616, "풀뽑기 5초 간격 테스트", a.previewAuto)
			}
			appendMenu.Call(m, mfPopup, preview, textPtr("동작 미리보기"))
		}
	}
	freqItems := []struct {
		id      uintptr
		label   string
		checked bool
	}{
		{821, "보통 (20~60초)", a.s.Frequency == 0},
		{822, "자주 (20~30초)", a.s.Frequency == 1},
		{823, "느긋하게 (45~60초)", a.s.Frequency == 2},
	}
	menuSubmenu(m, "랜덤 행동 빈도", freqItems)
	sizeItems := []struct {
		id      uintptr
		label   string
		checked bool
	}{}
	for _, n := range []int{75, 90, 100, 120, 140} {
		sizeItems = append(sizeItems, struct {
			id      uintptr
			label   string
			checked bool
		}{uintptr(700 + n), fmt.Sprintf("%d%%", n), n == a.s.Scale})
	}
	menuSubmenu(m, "캐릭터 크기", sizeItems)
	startItems := []struct {
		id      uintptr
		label   string
		checked bool
	}{}
	endItems := []struct {
		id      uintptr
		label   string
		checked bool
	}{}
	for i, v := range []string{"08:00", "08:30", "09:00", "09:30", "10:00"} {
		startItems = append(startItems, struct {
			id      uintptr
			label   string
			checked bool
		}{uintptr(310 + i), v, v == a.s.Start})
	}
	for i, v := range []string{"16:00", "17:00", "17:30", "18:00", "18:30", "19:00"} {
		endItems = append(endItems, struct {
			id      uintptr
			label   string
			checked bool
		}{uintptr(410 + i), v, v == a.s.End})
	}
	menuSubmenu(m, "출근 시간", startItems)
	menuSubmenu(m, "퇴근 시간", endItems)
	lunchItems := []struct {
		id      uintptr
		label   string
		checked bool
	}{
		{510, "11:30 ~ 12:30", a.s.LunchStart == "11:30"}, {511, "12:00 ~ 13:00", a.s.LunchStart == "12:00"}, {512, "12:30 ~ 13:30", a.s.LunchStart == "12:30"},
	}
	menuSubmenu(m, "점심시간", lunchItems)
	appendMenu.Call(m, mfSeparator, 0, 0)
	menuAppend(m, 614, "랜덤 행동 켜기", !a.s.QuietMode)
	menuAppend(m, 610, "항상 맨 위에 표시", a.s.AlwaysOnTop)
	menuAppend(m, 611, "Windows 시작 시 자동실행", a.startup)
	menuAppend(m, 612, "화면 오른쪽 아래로 이동", false)
	menuAppend(m, 613, "설정 파일 위치 안내", false)
	appendMenu.Call(m, mfSeparator, 0, 0)
	menuAppend(m, 999, "종료", false)
	pt := point{}
	getCursorPos.Call(uintptr(unsafe.Pointer(&pt)))
	setForegroundWindow.Call(a.hwnd)
	cmd, _, _ := trackPopupMenu.Call(m, tpmReturnCmd|tpmRightButton, uintptr(pt.X), uintptr(pt.Y), 0, a.hwnd, 0)
	postMessage.Call(a.hwnd, wmNull, 0, 0)
	destroyMenu.Call(m)
	a.command(int(cmd))
}
func (a *mate) command(n int) {
	switch {
	case n == 0:
		return
	case n == 775 || n == 790 || n == 800 || n == 820 || n == 840:
		scale := n - 700
		if scale == 75 || scale == 90 || scale == 100 || scale == 120 || scale == 140 {
			a.setSize(scale)
		}
	case n >= 310 && n <= 314:
		a.s.Start = []string{"08:00", "08:30", "09:00", "09:30", "10:00"}[n-310]
		a.saveSettings()
		a.render()
	case n >= 410 && n <= 415:
		a.s.End = []string{"16:00", "17:00", "17:30", "18:00", "18:30", "19:00"}[n-410]
		a.saveSettings()
		a.render()
	case n >= 510 && n <= 512:
		t := [][2]string{{"11:30", "12:30"}, {"12:00", "13:00"}, {"12:30", "13:30"}}[n-510]
		a.s.LunchStart = t[0]
		a.s.LunchEnd = t[1]
		a.saveSettings()
		a.render()
	case n >= 801 && n < 801+len(actions):
		a.flashUntil = time.Time{}
		now := time.Now()
		a.requestAction(actions[n-801].kind, now)
	case n == 615:
		now := time.Now()
		state := automaticStatus(now, a.nextAction, a.player, a.s, a.clips, a.previewAuto, 0)
		detail := fmt.Sprintf("%s\n시각: %s  |  시간대: %s\n다음 확인: %s  |  5초 테스트: %t",
			state.reason, now.Format("15:04:05"), statusAt(now, a.s).Label, a.nextAction.Format("15:04:05"), a.previewAuto)
		if _, ok := a.clips[Idle]; !ok {
			detail += "\n대기 이미지: idle_basic.png 미제공"
		}
		if a.packError != "" {
			detail += "\n동작 팩: " + a.packError
		}
		a.alert("자동 동작 진단", detail)
	case n == 616:
		a.previewAuto = !a.previewAuto
		a.nextAction = time.Now().Add(a.rest())
	case n >= 821 && n <= 823:
		a.s.Frequency = n - 821
		a.nextAction = time.Now().Add(a.rest())
		a.saveSettings()
	case n == 614:
		a.s.QuietMode = !a.s.QuietMode
		if !a.s.QuietMode {
			a.nextAction = time.Now().Add(a.rest())
		}
		a.saveSettings()
	case n == 610:
		a.setTop(!a.s.AlwaysOnTop)
	case n == 611:
		a.toggleStartup()
	case n == 612:
		a.resetPosition()
	case n == 613:
		a.alert("설정 파일", "퇴근시간 등을 자유롭게 지정하려면 메모장으로 아래 JSON 파일을 수정한 후 프로그램을 재실행하세요.\n\n"+a.configFile+"\n\n형식: 09:00 / 18:00")
	case n == 999:
		destroyWindow.Call(a.hwnd)
	}
}

func windowProc(hwnd uintptr, msg uint32, wParam, lParam uintptr) uintptr {
	a := app
	if a == nil {
		ret, _, _ := defWindowProc.Call(hwnd, uintptr(msg), wParam, lParam)
		return ret
	}
	switch msg {
	case wmTimer:
		a.frameTick()
		return 0
	case wmLButtonDown:
		a.dragging = true
		a.moved = false
		getCursorPos.Call(uintptr(unsafe.Pointer(&a.dragStart)))
		a.windowStart = point{int32(a.x), int32(a.y)}
		setCapture.Call(hwnd)
		return 0
	case wmMouseMove:
		if a.dragging {
			var p point
			getCursorPos.Call(uintptr(unsafe.Pointer(&p)))
			dx, dy := int(p.X-a.dragStart.X), int(p.Y-a.dragStart.Y)
			if dx*dx+dy*dy > 16 {
				a.moved = true
			}
			if a.moved {
				a.x = int(a.windowStart.X) + dx
				a.y = int(a.windowStart.Y) + dy
				setWindowPos.Call(hwnd, 0, uintptr(a.x), uintptr(a.y), 0, 0, swpNoSize|swpNoZOrder|swpNoActivate)
			}
		}
		return 0
	case wmLButtonUp:
		if a.dragging {
			a.dragging = false
			releaseCapture.Call()
			if a.moved {
				a.s.X = a.x
				a.s.Y = a.y
				a.s.Placed = true
				a.saveSettings()
			} else {
				labels := []string{"click1", "click2", "click3", "click4", "click5"}
				now := time.Now()
				kind := selectAction(choicesAt(now, a.s, true), a.clips, a.player.last, a.rng.Intn(10000))
				if kind != Idle {
					a.requestAction(kind, now)
				}
				a.flashLabel = labels[a.rng.Intn(len(labels))]
				a.flashUntil = time.Now().Add(3 * time.Second)
				a.render()
			}
		}
		return 0
	case wmContextMenu:
		a.contextMenu()
		return 0
	case wmClose:
		destroyWindow.Call(hwnd)
		return 0
	case wmDestroy:
		killTimer.Call(hwnd, 1)
		a.surface.close()
		postQuit.Call(0)
		return 0
	}
	ret, _, _ := defWindowProc.Call(hwnd, uintptr(msg), wParam, lParam)
	return ret
}
func run() error {
	runtime.LockOSThread()
	setDPIAware.Call()
	app = &mate{rng: rand.New(rand.NewSource(time.Now().UnixNano()))}
	app.loadSettings()
	app.loadAnimations()
	if err := app.loadImages(); err != nil {
		return err
	}
	if err := app.initSize(); err != nil {
		return err
	}
	app.defaultPosition()
	app.startup = isAutoRun()
	inst, _, _ := getModuleHandle.Call(0)
	className := syscall.StringToUTF16Ptr("KurimanjuMateWindowV31")
	wnd := wndClassEx{Size: uint32(unsafe.Sizeof(wndClassEx{})), WndProc: syscall.NewCallback(windowProc), Instance: inst, ClassName: uintptr(unsafe.Pointer(className))}
	ret, _, _ := registerClass.Call(uintptr(unsafe.Pointer(&wnd)))
	if ret == 0 {
		return fmt.Errorf("창 클래스 등록 실패")
	}
	ex := uintptr(exLayered | exToolWindow)
	if app.s.AlwaysOnTop {
		ex |= exTopmost
	}
	hwnd, _, _ := createWindow.Call(ex, uintptr(unsafe.Pointer(className)), textPtr("Kurimanju Mate 3.1"), wsPopup,
		uintptr(app.x), uintptr(app.y), uintptr(app.w), uintptr(app.h), 0, 0, inst, 0)
	if hwnd == 0 {
		return fmt.Errorf("Windows 위젯 창 생성 실패")
	}
	app.hwnd = hwnd
	showWindow.Call(hwnd, swShowNoActivate)
	updateWindow.Call(hwnd)
	now := time.Now()
	app.startAction(Idle, now)
	app.render()
	app.armTimer(time.Now())
	var m winMessage
	for {
		ret, _, err := getMessage.Call(uintptr(unsafe.Pointer(&m)), 0, 0, 0)
		if int32(ret) <= 0 {
			if int32(ret) == -1 {
				return fmt.Errorf("Windows 메시지 처리 오류: %v", err)
			}
			break
		}
		translateMessage.Call(uintptr(unsafe.Pointer(&m)))
		dispatchMessage.Call(uintptr(unsafe.Pointer(&m)))
	}
	return nil
}
func main() {
	if err := run(); err != nil {
		msg := strings.TrimSpace(err.Error())
		messageBox.Call(0, textPtr(msg), textPtr("쿠리만쥬 업무메이트 오류"), 0x10)
	}
}
