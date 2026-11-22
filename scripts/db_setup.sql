-- Create the database and tables for managing comics and pages
-- Run this file in your MySQL client (e.g., phpMyAdmin, MySQL Workbench, or command line).

CREATE DATABASE IF NOT EXISTS mis_comics CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE mis_comics;

-- Table that stores metadata for each comic title
CREATE TABLE IF NOT EXISTS comics (
  id INT AUTO_INCREMENT PRIMARY KEY,
  titulo VARCHAR(255) NOT NULL,
  url_comic TEXT NOT NULL,
  url_visor TEXT,
  autor VARCHAR(255),
  genero VARCHAR(255),
  fecha_public DATE,
  descripcion TEXT,
  n_paginas_web INT NULL COMMENT 'N de paginas que indica la web (pp-pagecount). 0 = sin indicador',
  fecha_descarga DATE NULL COMMENT 'Fecha en que download.py confirmo el disco',
  sitio VARCHAR(30) NOT NULL DEFAULT 'vercomicsporno' COMMENT 'vercomicsporno | vermangasporno | pawchive',
  estado ENUM('pendiente','descargado','duplicado') DEFAULT 'pendiente',
  UNIQUE KEY uq_url (url_comic),
  KEY idx_sitio (sitio),
  KEY idx_estado_sitio (estado, sitio),
  KEY idx_fecha_descarga (fecha_descarga)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Tags/etiquetas de cada comic (pp-taglist)
CREATE TABLE IF NOT EXISTS etiquetas (
  id INT AUTO_INCREMENT PRIMARY KEY,
  nombre VARCHAR(120) NOT NULL,
  slug VARCHAR(160) NULL,
  UNIQUE KEY uq_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS comic_etiquetas (
  comic_id INT NOT NULL,
  etiqueta_id INT NOT NULL,
  PRIMARY KEY (comic_id, etiqueta_id),
  FOREIGN KEY (comic_id) REFERENCES comics(id) ON DELETE CASCADE,
  FOREIGN KEY (etiqueta_id) REFERENCES etiquetas(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table that stores each page url for a comic
CREATE TABLE IF NOT EXISTS paginas (
  id INT AUTO_INCREMENT PRIMARY KEY,
  comic_id INT NOT NULL,
  numero INT NOT NULL,
  url_imagen TEXT NOT NULL,
  tipo ENUM('imagen','video','archivo') DEFAULT 'imagen' COMMENT 'Tipo de archivo',
  FOREIGN KEY (comic_id) REFERENCES comics(id) ON DELETE CASCADE,
  UNIQUE KEY uq_comic_pagina (comic_id, numero)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Índices adicionales para consultas frecuentes.
-- OJO: MySQL no admite "CREATE INDEX IF NOT EXISTS" (eso es de MariaDB),
-- así que se dejan comentados; ejecútalos SOLO si tu servidor no los tiene
-- ya (SHOW INDEX FROM comics;). Con el "CREATE TABLE IF NOT EXISTS" de
-- arriba no hacen falta en una instalación nueva.
-- CREATE INDEX idx_paginas_comic_numero ON paginas(comic_id, numero);
-- CREATE INDEX idx_comics_titulo_norm ON comics((LOWER(TRIM(REPLACE(REPLACE(titulo, '\r', ''), '\n', '')))));