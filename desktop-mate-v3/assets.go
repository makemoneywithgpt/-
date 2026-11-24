package main

import (
	"embed"
	"fmt"
	"image"
	"image/png"
	"path/filepath"
)

//go:embed assets/ui/*.png
var resourceFS embed.FS

func loadResource(path string) (image.Image, error) {
	f, err := resourceFS.Open(filepath.ToSlash(path))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	im, err := png.Decode(f)
	if err != nil {
		return nil, fmt.Errorf("decode %s: %w", path, err)
	}
	return im, nil
}
