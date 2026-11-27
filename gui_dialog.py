# -*- coding: utf-8 -*-
import os
import math
from qgis.PyQt import QtWidgets, QtCore, QtGui
from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsRasterLayer, QgsProject, QgsVectorLayer, QgsWkbTypes, QgsFeature, QgsGeometry, QgsPointXY, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsField
from .transform_engine import (
    apply_epoch_reduction_planimetric,
    apply_epoch_reduction_geodetic,
    BURSA_WOLF_PARAMS,
    geodetic_to_cartesian,
    cartesian_to_geodetic,
    apply_bursa_wolf_direct,
    apply_bursa_wolf_inverse,
    wgs84_to_lambert,
    lambert_to_wgs84,
    utm_to_hayford_geodetic,
    hayford_geodetic_to_utm,
    dms_to_decimal,
    sample_geoid_raster,
    sample_geoid_raster_with_name,
    convert_hayford_point_to_lambert, convert_point_to_lambert,
    A_HAYFORD, E2_HAYFORD, A_WGS84, E2_WGS84,
    CORS_CABO_VERDE, parse_rinex_header, process_rinex_baseline
)


def dms_to_str(val_deg, is_lat=True):
    abs_val = abs(val_deg)
    d = int(abs_val)
    m = int((abs_val - d) * 60)
    s = (abs_val - d - m / 60.0) * 3600.0
    hemi = ("N" if val_deg >= 0 else "S") if is_lat else ("E" if val_deg >= 0 else "W")
    return f"{d:02d}° {m:02d}' {s:05.2f} {hemi}"

class ImageCanvasView(QtWidgets.QGraphicsView):
    point_clicked = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None):
        super(ImageCanvasView, self).__init__(parent)
        self.scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self.scene)
        self.pixmap_item = None
        self.markers = []
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        
        # Cursor de precisão (Cruz de Mira / Crosshair)
        self.setCursor(QtCore.Qt.CrossCursor)
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        
        self.picking_mode = True
        self._is_panning = False
        self._pan_start = QtCore.QPoint()

    def load_image(self, file_path):
        self.scene.clear()
        self.markers = []
        pixmap = QtGui.QPixmap(file_path)
        if not pixmap.isNull():
            self.pixmap_item = self.scene.addPixmap(pixmap)
            self.setSceneRect(QtCore.QRectF(pixmap.rect()))
            self.resetTransform()
            self.fitInView(self.pixmap_item, QtCore.Qt.KeepAspectRatio)
            self.setCursor(QtCore.Qt.CrossCursor)
            return True
        return False

    def wheelEvent(self, event):
        if not self.pixmap_item:
            super(ImageCanvasView, self).wheelEvent(event)
            return
        
        zoom_in_factor = 1.25
        zoom_out_factor = 1.0 / zoom_in_factor

        old_pos = self.mapToScene(event.pos())
        if event.angleDelta().y() > 0:
            zoom_factor = zoom_in_factor
        else:
            zoom_factor = zoom_out_factor

        self.scale(zoom_factor, zoom_factor)
        new_pos = self.mapToScene(event.pos())
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())

    def mousePressEvent(self, event):
        if event.button() in (QtCore.Qt.RightButton, QtCore.Qt.MiddleButton):
            # Arrastar (Pan) com botão direito ou central do rato
            self._is_panning = True
            self._pan_start = event.pos()
            self.setCursor(QtCore.Qt.ClosedHandCursor)
            event.accept()
        elif event.button() == QtCore.Qt.LeftButton:
            # Clique esquerdo com Cruz de Mira para precisão cirúrgica no pixel
            scene_pos = self.mapToScene(event.pos())
            if self.pixmap_item and self.pixmap_item.boundingRect().contains(scene_pos) and self.picking_mode:
                px = scene_pos.x()
                py = scene_pos.y()
                self.point_clicked.emit(px, py)
            super(ImageCanvasView, self).mousePressEvent(event)
        else:
            super(ImageCanvasView, self).mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
        else:
            super(ImageCanvasView, self).mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() in (QtCore.Qt.RightButton, QtCore.Qt.MiddleButton) and self._is_panning:
            self._is_panning = False
            self.setCursor(QtCore.Qt.CrossCursor)
            event.accept()
        else:
            super(ImageCanvasView, self).mouseReleaseEvent(event)

    def zoom_in(self):
        if self.pixmap_item:
            self.scale(1.25, 1.25)

    def zoom_out(self):
        if self.pixmap_item:
            self.scale(0.8, 0.8)

    def fit_image(self):
        if self.pixmap_item:
            self.resetTransform()
            self.fitInView(self.pixmap_item, QtCore.Qt.KeepAspectRatio)

    def add_marker(self, px, py, label_str=""):
        pen = QtGui.QPen(QtGui.QColor(255, 0, 0), 2)
        r = 10
        l1 = self.scene.addLine(px - r, py, px + r, py, pen)
        l2 = self.scene.addLine(px, py - r, px, py + r, pen)
        t = self.scene.addText(label_str)
        t.setDefaultTextColor(QtGui.QColor(255, 0, 0))
        t.setPos(px + 4, py + 4)
        self.markers.extend([l1, l2, t])

    
    def clear_image(self):
        self.scene.clear()
        self.pixmap_item = None
        self.markers = []
        self.resetTransform()

    def clear_markers(self):
        for m in self.markers:
            self.scene.removeItem(m)
        self.markers = []


class AddGCPDialog(QtWidgets.QDialog):
    def __init__(self, px, py, island, parent=None):
        super(AddGCPDialog, self).__init__(parent)
        self.px = px
        self.py = py
        self.island = island
        self.setWindowTitle(f"Introduzir Coordenadas da Carta Antiga ({island})")
        self.setWindowFlags(QtCore.Qt.Window | QtCore.Qt.WindowMinMaxButtonsHint | QtCore.Qt.WindowCloseButtonHint)
        self.resize(460, 320)

        layout = QtWidgets.QVBoxLayout(self)

        self.info_lbl = QtWidgets.QLabel(f"<b>Ponto Clicado na Imagem:</b> Pixel X = {px:.1f}, Pixel Y = {py:.1f}")
        self.info_lbl.setStyleSheet("color: #1565C0; font-size: 12px;")
        layout.addWidget(self.info_lbl)

        form = QtWidgets.QFormLayout()

        self.radio_utm = QtWidgets.QRadioButton("Métricas UTM Hayford (Easting E, Northing N em m)")
        self.radio_dms = QtWidgets.QRadioButton("Geográficas Hayford (DMS: Lat N / Lon W)")
        self.radio_utm.setChecked(True)

        radio_box = QtWidgets.QVBoxLayout()
        radio_box.addWidget(self.radio_utm)
        radio_box.addWidget(self.radio_dms)
        form.addRow("Tipo de Coordenada:", radio_box)

        self.combo_datum_dlg = QtWidgets.QComboBox()
        self.combo_datum_dlg.addItems([
            "Datum Local Hayford 1924 (Carta Histórica IGP/INGT - 7 Parâmetros)",
            "WGS84 / ITRF96 (Grelha Moderna / GPS - Sem Translação)"
        ])
        form.addRow("Datum da Coordenada:", self.combo_datum_dlg)

        # Container UTM
        self.widget_utm = QtWidgets.QWidget()
        u_layout = QtWidgets.QFormLayout(self.widget_utm)
        self.input_e = QtWidgets.QLineEdit("220000.0")
        self.input_n = QtWidgets.QLineEdit("1660000.0")
        u_layout.addRow("Easting Hayford UTM (m):", self.input_e)
        u_layout.addRow("Northing Hayford UTM (m):", self.input_n)

        # Container DMS
        self.widget_dms = QtWidgets.QWidget()
        d_layout = QtWidgets.QFormLayout(self.widget_dms)
        lat_box = QtWidgets.QHBoxLayout()
        self.lat_d = QtWidgets.QLineEdit("14")
        self.lat_m = QtWidgets.QLineEdit("55")
        self.lat_s = QtWidgets.QLineEdit("30.0")
        lat_box.addWidget(self.lat_d)
        lat_box.addWidget(QtWidgets.QLabel("°"))
        lat_box.addWidget(self.lat_m)
        lat_box.addWidget(QtWidgets.QLabel("'"))
        lat_box.addWidget(self.lat_s)
        lat_box.addWidget(QtWidgets.QLabel('" N'))
        d_layout.addRow("Latitude:", lat_box)

        lon_box = QtWidgets.QHBoxLayout()
        self.lon_d = QtWidgets.QLineEdit("23")
        self.lon_m = QtWidgets.QLineEdit("31")
        self.lon_s = QtWidgets.QLineEdit("00.0")
        lon_box.addWidget(self.lon_d)
        lon_box.addWidget(QtWidgets.QLabel("°"))
        lon_box.addWidget(self.lon_m)
        lon_box.addWidget(QtWidgets.QLabel("'"))
        lon_box.addWidget(self.lon_s)
        lon_box.addWidget(QtWidgets.QLabel('" W'))
        d_layout.addRow("Longitude:", lon_box)

        layout.addLayout(form)
        layout.addWidget(self.widget_utm)
        layout.addWidget(self.widget_dms)
        self.widget_dms.hide()

        self.radio_utm.toggled.connect(self.toggle_mode)

        action_h_box = QtWidgets.QHBoxLayout()
        btn_min = QtWidgets.QPushButton(" Minimizar Janela")
        btn_min.setToolTip("Minimiza esta janela de introdução de coordenadas para ver o mapa por baixo")
        btn_min.clicked.connect(self.showMinimized)
        
        btn_box = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        
        action_h_box.addWidget(btn_min)
        action_h_box.addStretch()
        action_h_box.addWidget(btn_box)
        layout.addLayout(action_h_box)

    def toggle_mode(self):
        if self.radio_utm.isChecked():
            self.widget_utm.show()
            self.widget_dms.hide()
        else:
            self.widget_utm.hide()
            self.widget_dms.show()

    def update_pixel(self, px, py):
        self.px = px
        self.py = py
        self.info_lbl.setText(f"<b>Ponto Clicado na Imagem:</b> Pixel X = {px:.1f}, Pixel Y = {py:.1f}")

    def get_data(self):
        if self.radio_utm.isChecked():
            mode = "utm"
            v1_str = self.input_e.text().strip()
            v2_str = self.input_n.text().strip()
            e_val = float(v1_str)
            n_val = float(v2_str)
            datum_mode = "hayford" if self.combo_datum_dlg.currentIndex() == 0 else "wgs84"
            E_lam, N_lam, _, _ = convert_point_to_lambert(self.island, datum_mode, "utm", e_val, n_val)
        else:
            mode = "dms"
            datum_mode = "hayford" if self.combo_datum_dlg.currentIndex() == 0 else "wgs84"
            ld, lm, ls = float(self.lat_d.text()), float(self.lat_m.text()), float(self.lat_s.text())
            ond, onm, ons = float(self.lon_d.text()), float(self.lon_m.text()), float(self.lon_s.text())
            v1_str = f"{int(ld)} {int(lm)} {ls:.1f}"
            v2_str = f"{int(ond)} {int(onm)} {ons:.1f}"
            E_lam, N_lam, _, _ = convert_point_to_lambert(self.island, datum_mode, "dms", ld, lm, ls, ond, onm, ons)

        return self.px, self.py, v1_str, v2_str, E_lam, N_lam


class CVGeoTransformDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, iface=None):
        super(CVGeoTransformDialog, self).__init__(parent)
        self.iface = iface
        self.setWindowTitle("Kriol GeoBox v1.25.0 - Ferramentas Geodésicas de Cabo Verde (por Alex J. B. Andrade)")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint | QtCore.Qt.WindowMinimizeButtonHint)
        self.resize(1150, 750)

        main_layout = QtWidgets.QVBoxLayout(self)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setMovable(True)
        main_layout.addWidget(self.tabs)

        self.tab_transform = QtWidgets.QWidget()
        self.setup_transform_tab()
        self.tabs.addTab(self.tab_transform, "1. Transformação 7 Parâmetros (Bursa-Wolf IGP/INGT)")

        self.tab_ortometrica = QtWidgets.QWidget()
        self.setup_ortometrica_tab()
        self.tabs.addTab(self.tab_ortometrica, "2. Altimetria / Conversão Geoidal (H <-> h)")

        self.tab_georef = QtWidgets.QWidget()
        self.setup_georef_tab()
        self.tabs.addTab(self.tab_georef, "3. Georreferenciador Direto (Visualizador Interativo)")

        self.tab_epoch = QtWidgets.QWidget()
        self.setup_epoch_tab()
        self.tabs.addTab(self.tab_epoch, "4. Redução de Época ITRF96 (Deriva Tectónica)")



        footer = QtWidgets.QLabel("Conforme especificações geodésicas oficiais do IGP / INGT para Cabo Verde.")
        footer.setStyleSheet("color: #666; font-size: 11px;")
        main_layout.addWidget(footer)

    def toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def setup_transform_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_transform)
        self.tabs_t1 = QtWidgets.QTabWidget()
        self.tabs_t1.setMovable(True)
        layout.addWidget(self.tabs_t1)

        self.sub_t1_ponto = QtWidgets.QWidget()
        self.setup_sub_t1_ponto()
        self.tabs_t1.addTab(self.sub_t1_ponto, "1.1 Ponto Único")

        self.sub_t1_camada = QtWidgets.QWidget()
        self.setup_sub_t1_camada()
        self.tabs_t1.addTab(self.sub_t1_camada, "1.2 Camada Vetorial (Shapefile / GeoPackage)")

    def setup_sub_t1_ponto(self):
        layout = QtWidgets.QVBoxLayout(self.sub_t1_ponto)
        form = QtWidgets.QFormLayout()

        self.combo_island = QtWidgets.QComboBox()
        self.combo_island.addItems(list(BURSA_WOLF_PARAMS.keys()))
        form.addRow("Ilha de Origem:", self.combo_island)

        self.combo_mode = QtWidgets.QComboBox()
        self.combo_mode.addItems([
            "DIRETO: UTM Hayford (Local) -> Lambert Cabo Verde (EPSG:4826 / ITRF96)",
            "INVERSO: Lambert Cabo Verde (EPSG:4826) -> UTM Hayford (Local)"
        ])
        form.addRow("Sentido da Transformação:", self.combo_mode)

        self.combo_t1_fmt = QtWidgets.QComboBox()
        self.combo_t1_fmt.addItems([
            "Métricas (Easting / Northing em m)",
            "Geográficas Decimais (Latitude / Longitude em °)",
            "Geográficas DMS (Graus, Minutos, Segundos)"
        ])
        self.combo_t1_fmt.currentIndexChanged.connect(self.toggle_t1_fmt)
        form.addRow("Formato de Coordenada de Entrada:", self.combo_t1_fmt)

        layout.addLayout(form)

        # 1. Container Métrico
        self.widget_t1_metric = QtWidgets.QWidget()
        lay_m = QtWidgets.QFormLayout(self.widget_t1_metric)
        lay_m.setContentsMargins(0, 0, 0, 0)
        self.input_easting = QtWidgets.QLineEdit("220000.0")
        self.input_northing = QtWidgets.QLineEdit("1660000.0")
        lay_m.addRow("Easting (Métrico E / X em m):", self.input_easting)
        lay_m.addRow("Northing (Métrico N / Y em m):", self.input_northing)
        layout.addWidget(self.widget_t1_metric)

        # 2. Container Geográfico Decimal
        self.widget_t1_dec = QtWidgets.QWidget()
        lay_d = QtWidgets.QFormLayout(self.widget_t1_dec)
        lay_d.setContentsMargins(0, 0, 0, 0)
        self.input_t1_lat_dec = QtWidgets.QLineEdit("15.000063")
        self.input_t1_lon_dec = QtWidgets.QLineEdit("-23.603584")
        lay_d.addRow("Latitude (Graus Decimais °):", self.input_t1_lat_dec)
        lay_d.addRow("Longitude (Graus Decimais °):", self.input_t1_lon_dec)
        layout.addWidget(self.widget_t1_dec)
        self.widget_t1_dec.hide()

        # 3. Container Geográfico DMS
        self.widget_t1_dms = QtWidgets.QWidget()
        lay_dms = QtWidgets.QFormLayout(self.widget_t1_dms)
        lay_dms.setContentsMargins(0, 0, 0, 0)

        lat_b = QtWidgets.QHBoxLayout()
        self.t1_lat_d = QtWidgets.QLineEdit("15")
        self.t1_lat_m = QtWidgets.QLineEdit("00")
        self.t1_lat_s = QtWidgets.QLineEdit("00.2")
        lat_b.addWidget(self.t1_lat_d)
        lat_b.addWidget(QtWidgets.QLabel("°"))
        lat_b.addWidget(self.t1_lat_m)
        lat_b.addWidget(QtWidgets.QLabel("'"))
        lat_b.addWidget(self.t1_lat_s)
        lat_b.addWidget(QtWidgets.QLabel('" N'))
        lay_dms.addRow("Latitude DMS:", lat_b)

        lon_b = QtWidgets.QHBoxLayout()
        self.t1_lon_d = QtWidgets.QLineEdit("23")
        self.t1_lon_m = QtWidgets.QLineEdit("36")
        self.t1_lon_s = QtWidgets.QLineEdit("12.9")
        lon_b.addWidget(self.t1_lon_d)
        lon_b.addWidget(QtWidgets.QLabel("°"))
        lon_b.addWidget(self.t1_lon_m)
        lon_b.addWidget(QtWidgets.QLabel("'"))
        lon_b.addWidget(self.t1_lon_s)
        lon_b.addWidget(QtWidgets.QLabel('" W'))
        lay_dms.addRow("Longitude DMS:", lon_b)

        layout.addWidget(self.widget_t1_dms)
        self.widget_t1_dms.hide()

        form_h = QtWidgets.QFormLayout()

        self.combo_alt_type = QtWidgets.QComboBox()
        self.combo_alt_type.addItems([
            "Altitude Ortométrica (H em m - do Catálogo / Relatório IGP)",
            "Altitude Elipsoidal (h em m - Geodésica Direta GNSS)"
        ])
        form_h.addRow("Tipo de Altitude de Entrada:", self.combo_alt_type)

        self.input_h = QtWidgets.QLineEdit("717.47")
        self.lbl_h_input = QtWidgets.QLabel("Altitude Ortométrica (H em m):")
        form_h.addRow(self.lbl_h_input, self.input_h)

        self.input_n_geo = QtWidgets.QLineEdit("26.84")
        self.lbl_n_geo = QtWidgets.QLabel("Ondulação Geoidal (N em m):")
        form_h.addRow(self.lbl_n_geo, self.input_n_geo)

        layout.addLayout(form_h)

        self.combo_alt_type.currentIndexChanged.connect(self.update_t1_alt_fields)
        self.combo_island.currentIndexChanged.connect(self.update_t1_alt_fields)

        btn_calc = QtWidgets.QPushButton("Executar Transformação de 7 Parâmetros")
        btn_calc.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold; padding: 6px;")
        btn_calc.clicked.connect(self.run_transformation_ponto)
        layout.addWidget(btn_calc)

        self.result_text_ponto = QtWidgets.QTextEdit()
        self.result_text_ponto.setReadOnly(True)
        layout.addWidget(self.result_text_ponto)

    def toggle_t1_fmt(self, idx):
        if idx == 0:
            self.widget_t1_metric.show()
            self.widget_t1_dec.hide()
            self.widget_t1_dms.hide()
        elif idx == 1:
            self.widget_t1_metric.hide()
            self.widget_t1_dec.show()
            self.widget_t1_dms.hide()
        else:
            self.widget_t1_metric.hide()
            self.widget_t1_dec.hide()
            self.widget_t1_dms.show()

    def setup_sub_t1_camada(self):
        layout = QtWidgets.QVBoxLayout(self.sub_t1_camada)
        form = QtWidgets.QFormLayout()

        v_box = QtWidgets.QHBoxLayout()
        self.combo_vlayers_t1 = QtWidgets.QComboBox()
        self.refresh_vector_layers(self.combo_vlayers_t1)
        btn_ref_v1 = QtWidgets.QPushButton("Atualizar")
        btn_ref_v1.clicked.connect(lambda: self.refresh_vector_layers(self.combo_vlayers_t1))
        v_box.addWidget(self.combo_vlayers_t1)
        v_box.addWidget(btn_ref_v1)
        form.addRow("Camada Vetorial no QGIS:", v_box)

        f_box = QtWidgets.QHBoxLayout()
        self.input_file_v1 = QtWidgets.QLineEdit()
        self.input_file_v1.setPlaceholderText("Ou selecione um ficheiro Shapefile / GeoPackage (.shp / .gpkg)")
        btn_b_v1 = QtWidgets.QPushButton("Procurar...")
        btn_b_v1.clicked.connect(self.browse_vector_v1)
        f_box.addWidget(self.input_file_v1)
        f_box.addWidget(btn_b_v1)
        form.addRow("Ficheiro de Entrada:", f_box)

        self.combo_island_v1 = QtWidgets.QComboBox()
        self.combo_island_v1.addItems(list(BURSA_WOLF_PARAMS.keys()))
        form.addRow("Ilha de Origem:", self.combo_island_v1)

        self.combo_mode_v1 = QtWidgets.QComboBox()
        self.combo_mode_v1.addItems([
            "DIRETO: UTM Hayford (Local) -> Lambert Cabo Verde (EPSG:4826)",
            "INVERSO: Lambert Cabo Verde (EPSG:4826) -> UTM Hayford (Local)"
        ])
        form.addRow("Sentido:", self.combo_mode_v1)

        layout.addLayout(form)

        btn_run_v1 = QtWidgets.QPushButton("Transformar Camada Vetorial Inteira")
        btn_run_v1.setStyleSheet("background-color: #1565C0; color: white; font-weight: bold; padding: 6px;")
        btn_run_v1.clicked.connect(self.run_transformation_camada)
        layout.addWidget(btn_run_v1)

        self.result_text_camada = QtWidgets.QTextEdit()
        self.result_text_camada.setReadOnly(True)
        layout.addWidget(self.result_text_camada)

    def setup_ortometrica_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_ortometrica)
        self.tabs_t2 = QtWidgets.QTabWidget()
        self.tabs_t2.setMovable(True)
        layout.addWidget(self.tabs_t2)

        self.sub_t2_ponto = QtWidgets.QWidget()
        self.setup_sub_t2_ponto()
        self.tabs_t2.addTab(self.sub_t2_ponto, "2.1 Ponto Único WGS84")

        self.sub_t2_camada = QtWidgets.QWidget()
        self.setup_sub_t2_camada()
        self.tabs_t2.addTab(self.sub_t2_camada, "2.2 Camada Vetorial WGS84")

    def setup_sub_t2_ponto(self):
        layout = QtWidgets.QVBoxLayout(self.sub_t2_ponto)
        form = QtWidgets.QFormLayout()

        # 1. Sentido da Conversão Altimétrica
        self.combo_orto_mode_ponto = QtWidgets.QComboBox()
        self.combo_orto_mode_ponto.addItems([
            "Direto: Altitude Elipsoidal (h) -> Altitude Ortométrica (H = h - N)",
            "Inverso: Altitude Ortométrica (H) -> Altitude Elipsoidal (h = H + N)"
        ])
        self.combo_orto_mode_ponto.currentIndexChanged.connect(self.update_t2_mode_labels)
        form.addRow("Sentido da Conversão Altimétrica:", self.combo_orto_mode_ponto)

        # 2. Selector de Sistema de Coordenadas de Entrada
        self.combo_orto_crs_ponto = QtWidgets.QComboBox()
        self.combo_orto_crs_ponto.addItems([
            "Geográficas WGS84 (EPSG:4326 - Latitude / Longitude em °)",
            "Lambert Cabo Verde (EPSG:4826 - Easting X / Northing Y em m)",
            "Hayford UTM Fuso 26N (Métrico E / N em m - Fogo, Brava, Santo Antão, S. Vicente, S. Nicolau)",
            "Hayford UTM Fuso 27N (Métrico E / N em m - Santiago, Sal, Boa Vista, Maio)",
            "Hayford Geográficas (Latitude / Longitude em °)"
        ])
        form.addRow("Sistema de Coordenadas de Entrada:", self.combo_orto_crs_ponto)

        # 3. Selector de Ilha de Referência (para Hayford -> Bursa-Wolf)
        self.combo_orto_island_ponto = QtWidgets.QComboBox()
        self.combo_orto_island_ponto.addItems(list(BURSA_WOLF_PARAMS.keys()))
        self.lbl_orto_island = QtWidgets.QLabel("Ilha de Origem / Referência:")
        form.addRow(self.lbl_orto_island, self.combo_orto_island_ponto)

        self.combo_orto_crs_ponto.currentIndexChanged.connect(self.toggle_orto_crs_ponto)
        self.combo_orto_island_ponto.currentIndexChanged.connect(self.update_t2_default_n)

        # Container 0: WGS84
        self.widget_orto_wgs = QtWidgets.QWidget()
        lay_wgs = QtWidgets.QFormLayout(self.widget_orto_wgs)
        lay_wgs.setContentsMargins(0, 0, 0, 0)
        self.input_wgs_lat = QtWidgets.QLineEdit("14.925138")
        lay_wgs.addRow("Latitude WGS84 (Graus Decimais °):", self.input_wgs_lat)
        self.input_wgs_lon = QtWidgets.QLineEdit("-23.520000")
        lay_wgs.addRow("Longitude WGS84 (Graus Decimais °):", self.input_wgs_lon)

        # Container 1: Lambert
        self.widget_orto_lambert = QtWidgets.QWidget()
        lay_lam = QtWidgets.QFormLayout(self.widget_orto_lambert)
        lay_lam.setContentsMargins(0, 0, 0, 0)
        self.input_lambert_e = QtWidgets.QLineEdit("211600.0")
        lay_lam.addRow("Easting Lambert E (m):", self.input_lambert_e)
        self.input_lambert_n = QtWidgets.QLineEdit("113200.0")
        lay_lam.addRow("Northing Lambert N (m):", self.input_lambert_n)

        # Container 2 & 3: Hayford UTM
        self.widget_orto_hayford_utm = QtWidgets.QWidget()
        lay_h_utm = QtWidgets.QFormLayout(self.widget_orto_hayford_utm)
        lay_h_utm.setContentsMargins(0, 0, 0, 0)
        self.input_h_utm_e = QtWidgets.QLineEdit("772493.600")
        lay_h_utm.addRow("Easting Hayford UTM E (m):", self.input_h_utm_e)
        self.input_h_utm_n = QtWidgets.QLineEdit("1653214.330")
        lay_h_utm.addRow("Northing Hayford UTM N (m):", self.input_h_utm_n)

        # Container 4: Hayford Geográficas
        self.widget_orto_hayford_geo = QtWidgets.QWidget()
        lay_h_geo = QtWidgets.QFormLayout(self.widget_orto_hayford_geo)
        lay_h_geo.setContentsMargins(0, 0, 0, 0)
        self.input_h_geo_lat = QtWidgets.QLineEdit("14.925000")
        lay_h_geo.addRow("Latitude Hayford (Graus Decimais °):", self.input_h_geo_lat)
        self.input_h_geo_lon = QtWidgets.QLineEdit("-23.520000")
        lay_h_geo.addRow("Longitude Hayford (Graus Decimais °):", self.input_h_geo_lon)

        layout.addLayout(form)
        layout.addWidget(self.widget_orto_wgs)
        layout.addWidget(self.widget_orto_lambert)
        layout.addWidget(self.widget_orto_hayford_utm)
        layout.addWidget(self.widget_orto_hayford_geo)

        self.widget_orto_lambert.hide()
        self.widget_orto_hayford_utm.hide()
        self.widget_orto_hayford_geo.hide()
        self.lbl_orto_island.hide()
        self.combo_orto_island_ponto.hide()

        # Form Bottom: Altitude & Ondulação Geoidal
        form_bottom = QtWidgets.QFormLayout()

        self.lbl_alt_input_t2 = QtWidgets.QLabel("Altitude Elipsoidal GNSS (h em m):")
        self.input_wgs_h = QtWidgets.QLineEdit("50.0")
        form_bottom.addRow(self.lbl_alt_input_t2, self.input_wgs_h)

        self.input_manual_n_t2 = QtWidgets.QLineEdit("47.50")
        form_bottom.addRow("Ondulação Geoidal Manual / Média (N em m):", self.input_manual_n_t2)

        geoid_box = QtWidgets.QHBoxLayout()
        self.input_geoid_t2 = QtWidgets.QLineEdit()
        self.input_geoid_t2.setPlaceholderText("Selecione um ficheiro raster geoidal (.tif / .gtx / .asc / .grd) [Opcional]...")
        btn_b_g2 = QtWidgets.QPushButton("Procurar Raster...")
        btn_b_g2.clicked.connect(self.browse_geoid_t2)
        geoid_box.addWidget(self.input_geoid_t2)
        geoid_box.addWidget(btn_b_g2)
        form_bottom.addRow("Ficheiro Raster Geoidal (N em m) [Opcional]:", geoid_box)

        layout.addLayout(form_bottom)

        self.btn_calc_orto = QtWidgets.QPushButton("Calcular Altitude Ortométrica (H = h - N)")
        self.btn_calc_orto.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold; padding: 6px;")
        self.btn_calc_orto.clicked.connect(self.run_ortometrica_ponto)
        layout.addWidget(self.btn_calc_orto)

        self.result_orto_ponto = QtWidgets.QTextEdit()
        self.result_orto_ponto.setReadOnly(True)
        layout.addWidget(self.result_orto_ponto)

    def toggle_orto_crs_ponto(self, idx):
        self.widget_orto_wgs.hide()
        self.widget_orto_lambert.hide()
        self.widget_orto_hayford_utm.hide()
        self.widget_orto_hayford_geo.hide()

        if idx == 0:
            self.widget_orto_wgs.show()
            self.lbl_orto_island.hide()
            self.combo_orto_island_ponto.hide()
        elif idx == 1:
            self.widget_orto_lambert.show()
            self.lbl_orto_island.hide()
            self.combo_orto_island_ponto.hide()
        elif idx in (2, 3):
            self.widget_orto_hayford_utm.show()
            self.lbl_orto_island.show()
            self.combo_orto_island_ponto.show()
        else:
            self.widget_orto_hayford_geo.show()
            self.lbl_orto_island.show()
            self.combo_orto_island_ponto.show()
        self.update_t2_default_n()

    def update_t2_mode_labels(self):
        mode = self.combo_orto_mode_ponto.currentIndex()
        if mode == 0:
            self.lbl_alt_input_t2.setText("Altitude Elipsoidal GNSS (h em m):")
            self.btn_calc_orto.setText("Calcular Altitude Ortométrica (H = h - N)")
        else:
            self.lbl_alt_input_t2.setText("Altitude Ortométrica (H em m):")
            self.btn_calc_orto.setText("Calcular Altitude Elipsoidal (h = H + N)")

    def update_t2_default_n(self):
        island = self.combo_orto_island_ponto.currentText()
        params = BURSA_WOLF_PARAMS.get(island, {})
        def_n = params.get("default_N", 47.50)
        self.input_manual_n_t2.setText(f"{def_n:.2f}")

    def setup_sub_t2_camada(self):
        layout = QtWidgets.QVBoxLayout(self.sub_t2_camada)
        form = QtWidgets.QFormLayout()

        self.combo_orto_mode_camada = QtWidgets.QComboBox()
        self.combo_orto_mode_camada.addItems([
            "Direto: Altitude Elipsoidal (h) -> Altitude Ortométrica (H = h - N)",
            "Inverso: Altitude Ortométrica (H) -> Altitude Elipsoidal (h = H + N)"
        ])
        form.addRow("Sentido da Conversão Altimétrica:", self.combo_orto_mode_camada)

        v_box2 = QtWidgets.QHBoxLayout()
        self.combo_vlayers_t2 = QtWidgets.QComboBox()
        self.refresh_vector_layers(self.combo_vlayers_t2)
        btn_ref_v2 = QtWidgets.QPushButton("Atualizar")
        btn_ref_v2.clicked.connect(lambda: self.refresh_vector_layers(self.combo_vlayers_t2))
        v_box2.addWidget(self.combo_vlayers_t2)
        v_box2.addWidget(btn_ref_v2)
        form.addRow("Camada Vetorial no QGIS:", v_box2)

        f_box2 = QtWidgets.QHBoxLayout()
        self.input_file_v2 = QtWidgets.QLineEdit()
        self.input_file_v2.setPlaceholderText("Ou selecione um ficheiro Shapefile / GeoPackage (.shp / .gpkg)")
        btn_b_v2 = QtWidgets.QPushButton("Procurar...")
        btn_b_v2.clicked.connect(self.browse_vector_v2)
        f_box2.addWidget(self.input_file_v2)
        f_box2.addWidget(btn_b_v2)
        form.addRow("Ficheiro de Entrada:", f_box2)

        geoid_box2 = QtWidgets.QHBoxLayout()
        self.input_geoid_t2_camada = QtWidgets.QLineEdit()
        self.input_geoid_t2_camada.setPlaceholderText("Selecione um ficheiro raster geoidal (.tif / .gtx / .asc / .grd) [Opcional]...")
        btn_b_g2c = QtWidgets.QPushButton("Procurar Raster...")
        btn_b_g2c.clicked.connect(self.browse_geoid_t2_camada)
        geoid_box2.addWidget(self.input_geoid_t2_camada)
        geoid_box2.addWidget(btn_b_g2c)
        form.addRow("Ficheiro Raster Geoidal (N em m) [Opcional]:", geoid_box2)

        self.input_manual_n_camada = QtWidgets.QLineEdit("47.50")
        form.addRow("Ondulação Geoidal Manual / Média (N em m):", self.input_manual_n_camada)

        self.combo_h_field = QtWidgets.QComboBox()
        form.addRow("Campo com Altitude de Entrada:", self.combo_h_field)

        self.combo_orto_op = QtWidgets.QComboBox()
        self.combo_orto_op.addItems([
            "Adicionar Campos H_orto, h_elips e N_geoid na Tabela de Atributos",
            "Atualizar Coordenada Z da Geometria (Z = Altitude Calculada)"
        ])
        form.addRow("Ação na Camada:", self.combo_orto_op)

        self.combo_vlayers_t2.currentIndexChanged.connect(self.update_h_fields)
        self.input_file_v2.textChanged.connect(self.update_h_fields)

        layout.addLayout(form)

        btn_run_orto_c = QtWidgets.QPushButton("Calcular Conversão Altimétrica na Camada Vetorial")
        btn_run_orto_c.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold; padding: 6px;")
        btn_run_orto_c.clicked.connect(self.run_ortometrica_camada)
        layout.addWidget(btn_run_orto_c)

        self.result_orto_camada = QtWidgets.QTextEdit()
        self.result_orto_camada.setReadOnly(True)
        layout.addWidget(self.result_orto_camada)


    def setup_georef_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_georef)

        form = QtWidgets.QFormLayout()

        r_box = QtWidgets.QHBoxLayout()
        self.input_raster_path = QtWidgets.QLineEdit()
        self.input_raster_path.setPlaceholderText("Selecione uma imagem de mapa antigo (.tif, .png, .jpg)")
        btn_b_r = QtWidgets.QPushButton("Procurar e Carregar Imagem...")
        btn_b_r.clicked.connect(self.browse_raster_file)
        btn_clear_r = QtWidgets.QPushButton("Remover Imagem")
        btn_clear_r.setStyleSheet("background-color: #C62828; color: white; font-weight: bold;")
        btn_clear_r.clicked.connect(self.remove_raster_image)
        r_box.addWidget(self.input_raster_path)
        r_box.addWidget(btn_b_r)
        r_box.addWidget(btn_clear_r)
        form.addRow("Imagem Raster da Carta:", r_box)

        self.georef_island = QtWidgets.QComboBox()
        self.georef_island.addItems(list(BURSA_WOLF_PARAMS.keys()))
        form.addRow("Ilha do Mapa Antigo:", self.georef_island)

        self.georef_datum_combo = QtWidgets.QComboBox()
        self.georef_datum_combo.addItems([
            "Datum Local Hayford 1924 (Carta Histórica IGP/INGT - 7 Parâmetros)",
            "WGS84 / ITRF96 (Grelha Moderna / GPS - Sem Translação)"
        ])
        form.addRow("Datum das Coordenadas lidas:", self.georef_datum_combo)

        out_r_box = QtWidgets.QHBoxLayout()
        self.input_out_geotiff = QtWidgets.QLineEdit()
        self.input_out_geotiff.setPlaceholderText("Caminho do GeoTIFF de saída em Lambert EPSG:4826")
        btn_b_out_r = QtWidgets.QPushButton("Salvar em...")
        btn_b_out_r.clicked.connect(self.browse_out_geotiff)
        out_r_box.addWidget(self.input_out_geotiff)
        out_r_box.addWidget(btn_b_out_r)
        form.addRow("GeoTIFF de Saída:", out_r_box)

        self.georef_algo_combo = QtWidgets.QComboBox()
        self.georef_algo_combo.addItems([
            "Polinomial 1ª Ordem (Afim - Modelo Padrão)",
            "Polinomial 2ª Ordem (Superfície Suave)",
            "Thin Plate Spline (TPS - Deformação Local)"
        ])
        form.addRow("Algoritmo de Retificação (Modelo):", self.georef_algo_combo)

        self.georef_resample_combo = QtWidgets.QComboBox()
        self.georef_resample_combo.addItems([
            "Bilinear (Suave - Recomendado para Cartas / Fotografias)",
            "Nearest Neighbor / Vizinho Mais Próximo (Preserva Valores Originais)",
            "Cubic / Cúbico (Alta Qualidade / Detalhe)",
            "Cubic Spline / Spline Cúbica (Suavização Avançada)",
            "Lanczos (Nitidez Extrema / Filtro Sinc)"
        ])
        form.addRow("Método de Reamostragem (Resampling):", self.georef_resample_combo)

        layout.addLayout(form)

        # Separador Vertical Principal (Divide o Visualizador/Tabela do Painel de Ações Inferior)
        v_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        # Separador Horizontal (Divide o Visualizador da Imagem da Tabela de GCPs)
        h_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        self.view_container = QtWidgets.QWidget()
        view_lay = QtWidgets.QVBoxLayout(self.view_container)
        view_lay.setContentsMargins(0,0,0,0)
        view_lay.addWidget(QtWidgets.QLabel("<b>Visualizador da Imagem (Cruz de Mira [+] Esquerdo | Mão [✋] Botão Direito / Central + Arrastar):</b>"))
        
        zoom_bar = QtWidgets.QHBoxLayout()
        btn_zoom_in = QtWidgets.QPushButton("Zoom +")
        btn_zoom_in.clicked.connect(lambda: self.canvas_view.zoom_in())
        btn_zoom_out = QtWidgets.QPushButton("Zoom -")
        btn_zoom_out.clicked.connect(lambda: self.canvas_view.zoom_out())
        btn_zoom_fit = QtWidgets.QPushButton("Ajustar (Fit)")
        btn_zoom_fit.clicked.connect(lambda: self.canvas_view.fit_image())
        
        zoom_bar.addWidget(btn_zoom_in)
        zoom_bar.addWidget(btn_zoom_out)
        zoom_bar.addWidget(btn_zoom_fit)
        zoom_bar.addStretch()
        view_lay.addLayout(zoom_bar)

        self.canvas_view = ImageCanvasView()
        self.canvas_view.point_clicked.connect(self.on_canvas_clicked)
        view_lay.addWidget(self.canvas_view)
        h_splitter.addWidget(self.view_container)

        table_container = QtWidgets.QWidget()
        table_lay = QtWidgets.QVBoxLayout(table_container)
        table_lay.setContentsMargins(0,0,0,0)
        table_lay.addWidget(QtWidgets.QLabel("<b>Tabela de Pontos de Controlo (GCPs):</b>"))

        self.gcp_table = QtWidgets.QTableWidget(0, 6)
        self.gcp_table.setHorizontalHeaderLabels([
            "Pixel X", "Pixel Y", "Entrada (E / Lat)", "Entrada (N / Lon)",
            "Lambert Destino E (m)", "Lambert Destino N (m)"
        ])
        self.gcp_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        table_lay.addWidget(self.gcp_table)

        btn_table_box = QtWidgets.QHBoxLayout()
        btn_add_gcp = QtWidgets.QPushButton("+ Adicionar GCP Manual")
        btn_add_gcp.clicked.connect(self.add_gcp_row)
        btn_rem_gcp = QtWidgets.QPushButton("- Remover GCP")
        btn_rem_gcp.clicked.connect(self.rem_gcp_row)
        btn_export_gcp = QtWidgets.QPushButton(" Exportar GCPs (Excel / CSV)")
        btn_export_gcp.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold;")
        btn_export_gcp.clicked.connect(self.export_gcps_to_excel)
        btn_table_box.addWidget(btn_add_gcp)
        btn_table_box.addWidget(btn_rem_gcp)
        btn_table_box.addWidget(btn_export_gcp)
        table_lay.addLayout(btn_table_box)

        h_splitter.addWidget(table_container)
        h_splitter.setSizes([600, 450])
        v_splitter.addWidget(h_splitter)

        # Painel Inferior de Ações, Barra de Progresso e Log
        bottom_actions_container = QtWidgets.QWidget()
        bottom_lay = QtWidgets.QVBoxLayout(bottom_actions_container)
        bottom_lay.setContentsMargins(0, 4, 0, 0)

        self.georef_progress_bar = QtWidgets.QProgressBar()
        self.georef_progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #1565C0;
                border-radius: 4px;
                text-align: center;
                font-weight: bold;
                height: 22px;
                background-color: #E0E0E0;
            }
            QProgressBar::chunk {
                background-color: #2E7D32;
                border-radius: 3px;
            }
        """)
        self.georef_progress_bar.setValue(0)
        self.georef_progress_bar.setVisible(False)
        bottom_lay.addWidget(self.georef_progress_bar)

        btn_actions_box = QtWidgets.QHBoxLayout()
        self.btn_calc_gcps = QtWidgets.QPushButton("1. Recalcular Todos os GCPs para Lambert")
        self.btn_calc_gcps.setStyleSheet("background-color: #1565C0; color: white; font-weight: bold; padding: 6px;")
        self.btn_calc_gcps.clicked.connect(self.calculate_all_gcps)

        self.btn_warp = QtWidgets.QPushButton("2. Georreferenciar Imagem (GDAL Warp)")
        self.btn_warp.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold; padding: 6px;")
        self.btn_warp.clicked.connect(self.warp_image)

        btn_actions_box.addWidget(self.btn_calc_gcps)
        btn_actions_box.addWidget(self.btn_warp)
        bottom_lay.addLayout(btn_actions_box)

        self.check_load_qgis = QtWidgets.QCheckBox("Carregar GeoTIFF retificado automaticamente no QGIS")
        self.check_load_qgis.setChecked(True)
        bottom_lay.addWidget(self.check_load_qgis)

        self.georef_log = QtWidgets.QTextEdit()
        self.georef_log.setReadOnly(True)
        bottom_lay.addWidget(self.georef_log)

        v_splitter.addWidget(bottom_actions_container)
        v_splitter.setSizes([500, 250])
        layout.addWidget(v_splitter)

    

    def export_gcps_to_excel(self):
        if self.gcp_table.rowCount() == 0:
            QtWidgets.QMessageBox.warning(self, "Aviso", "A tabela de GCPs está vazia! Adicione ou calcule pontos antes de exportar.")
            return

        filename, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Exportar Tabela de Pontos de Controlo (GCPs)",
            "GCPs_Lambert_EPSG4826.xlsx",
            "Ficheiro Excel (*.xlsx);;Ficheiro CSV (*.csv);;Todos os Ficheiros (*)"
        )

        if not filename:
            return

        headers = [self.gcp_table.horizontalHeaderItem(c).text() for c in range(self.gcp_table.columnCount())]
        data = []
        for r in range(self.gcp_table.rowCount()):
            row_data = []
            for c in range(self.gcp_table.columnCount()):
                item = self.gcp_table.item(r, c)
                val = item.text() if item else ""
                row_data.append(val)
            data.append(row_data)

        ext = os.path.splitext(filename)[1].lower()

        if ext == ".xlsx" or "xlsx" in selected_filter.lower():
            if not filename.lower().endswith(".xlsx"):
                filename += ".xlsx"
            try:
                import openpyxl
                from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "GCPs Lambert EPSG4826"

                header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
                header_fill = PatternFill(start_color="1565C0", end_color="1565C0", fill_type="solid")
                align_center = Alignment(horizontal="center", vertical="center")

                ws.append(headers)
                for col_idx in range(1, len(headers) + 1):
                    cell = ws.cell(row=1, column=col_idx)
                    cell.font = header_font
                    cell.fill = header_fill
                    cell.alignment = align_center

                for row in data:
                    typed_row = []
                    for val in row:
                        try:
                            num = float(val)
                            typed_row.append(num)
                        except ValueError:
                            typed_row.append(val)
                    ws.append(typed_row)

                for col in ws.columns:
                    max_len = max(len(str(cell.value or '')) for cell in col)
                    col_letter = openpyxl.utils.get_column_letter(col[0].column)
                    ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

                wb.save(filename)
                self.georef_log.append(f"\nÉXITO: Tabela de {len(data)} GCPs exportada com sucesso para Excel:\n{filename}")
                QtWidgets.QMessageBox.information(self, "Sucesso", f"Tabela de GCPs exportada com sucesso para:\n{filename}")
                return
            except Exception as e_xlsx:
                # Fallback to CSV if openpyxl is not available in environment
                self.georef_log.append("Aviso openpyxl: " + str(e_xlsx))

        if not filename.lower().endswith(".csv") and not filename.lower().endswith(".xlsx"):
            filename += ".csv"
        try:
            import csv
            with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(headers)
                for r in data:
                    writer.writerow(r)
            self.georef_log.append(f"\nÉXITO: Tabela de {len(data)} GCPs exportada com sucesso para CSV/Excel:\n{filename}")
            QtWidgets.QMessageBox.information(self, "Sucesso", f"Tabela de GCPs exportada com sucesso para:\n{filename}")
        except Exception as e_csv:
            QtWidgets.QMessageBox.critical(self, "Erro", f"Falha ao exportar GCPs: {str(e_csv)}")


    def remove_raster_image(self):
        self.input_raster_path.clear()
        self.input_out_geotiff.clear()
        self.canvas_view.clear_image()
        self.gcp_table.setRowCount(0)
        self.georef_log.setText("Imagem e Pontos de Controlo (GCPs) removidos.")



    def browse_raster_file(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Selecionar Carta Escaneada", "", "Imagens (*.tif *.tiff *.png *.jpg *.jpeg)")
        if filename:
            self.input_raster_path.setText(filename)
            base, ext = os.path.splitext(filename)
            self.input_out_geotiff.setText(f"{base}_lambert_epsg4826.tif")
            if self.canvas_view.load_image(filename):
                self.georef_log.setText(f"Imagem carregada no visualizador interativo: {os.path.basename(filename)}\nClique num ponto com a Cruz de Mira (+) para capturar coordenadas.")
            else:
                self.georef_log.setText("Aviso: Não foi possível carregar a imagem no visualizador.")

    def browse_out_geotiff(self):
        filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Salvar GeoTIFF Georreferenciado", "", "GeoTIFF (*.tif)")
        if filename:
            self.input_out_geotiff.setText(filename)

    def on_canvas_clicked(self, px, py):
        island = self.georef_island.currentText()
        if hasattr(self, 'active_gcp_dlg') and self.active_gcp_dlg and self.active_gcp_dlg.isVisible():
            self.active_gcp_dlg.update_pixel(px, py)
            self.active_gcp_dlg.raise_()
            self.active_gcp_dlg.activateWindow()
        else:
            self.active_gcp_dlg = AddGCPDialog(px, py, island, self)
            self.active_gcp_dlg.setWindowModality(QtCore.Qt.NonModal)
            self.active_gcp_dlg.accepted.connect(lambda: self.on_gcp_accepted(self.active_gcp_dlg))
            self.active_gcp_dlg.show()
            self.active_gcp_dlg.raise_()
            self.active_gcp_dlg.activateWindow()

    def on_gcp_accepted(self, dlg):
        try:
            px, py, v1_str, v2_str, E_lam, N_lam = dlg.get_data()
            row = self.gcp_table.rowCount()
            self.gcp_table.insertRow(row)
            self.gcp_table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"{px:.1f}"))
            self.gcp_table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{py:.1f}"))
            self.gcp_table.setItem(row, 2, QtWidgets.QTableWidgetItem(v1_str))
            self.gcp_table.setItem(row, 3, QtWidgets.QTableWidgetItem(v2_str))
            self.gcp_table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{E_lam:.3f}"))
            self.gcp_table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{N_lam:.3f}"))

            self.canvas_view.add_marker(px, py, f"GCP {row+1}")
            self.georef_log.append(f"GCP {row+1} Adicionado: Pixel({px:.1f},{py:.1f}) -> Lambert E={E_lam:.3f} m, N={N_lam:.3f} m")
        except Exception as e:
            self.georef_log.append(f"Erro ao adicionar GCP: {str(e)}")
        finally:
            self.active_gcp_dlg = None

    def add_gcp_row(self):
        row = self.gcp_table.rowCount()
        self.gcp_table.insertRow(row)
        self.gcp_table.setItem(row, 0, QtWidgets.QTableWidgetItem("0.0"))
        self.gcp_table.setItem(row, 1, QtWidgets.QTableWidgetItem("0.0"))
        self.gcp_table.setItem(row, 2, QtWidgets.QTableWidgetItem("220000.0"))
        self.gcp_table.setItem(row, 3, QtWidgets.QTableWidgetItem("1660000.0"))
        self.gcp_table.setItem(row, 4, QtWidgets.QTableWidgetItem("0.0"))
        self.gcp_table.setItem(row, 5, QtWidgets.QTableWidgetItem("0.0"))

    def rem_gcp_row(self):
        curr = self.gcp_table.currentRow()
        if curr >= 0:
            self.gcp_table.removeRow(curr)
            self.refresh_markers()

    def refresh_markers(self):
        self.canvas_view.clear_markers()
        for r in range(self.gcp_table.rowCount()):
            try:
                px = float(self.gcp_table.item(r, 0).text())
                py = float(self.gcp_table.item(r, 1).text())
                self.canvas_view.add_marker(px, py, f"GCP {r+1}")
            except Exception as e_marker:
                self.georef_log.append("Aviso marker: " + str(e_marker))

    def calculate_all_gcps(self):
        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            self.btn_calc_gcps.setText("⏳ A Recalcular GCPs...")
            self.btn_calc_gcps.setEnabled(False)
            self.btn_warp.setEnabled(False)

            total_rows = self.gcp_table.rowCount()
            self.georef_progress_bar.setRange(0, max(1, total_rows))
            self.georef_progress_bar.setValue(0)
            self.georef_progress_bar.setFormat("A recalcular GCPs: %v/%m (%p%)")
            self.georef_progress_bar.setVisible(True)
            QtWidgets.QApplication.processEvents()

            island = self.georef_island.currentText()
            log = f"=== RECALCULO DOS GCPS PARA LAMBERT (EPSG:4826) - ILHA: {island} ===\n"

            for row in range(total_rows):
                px_item = self.gcp_table.item(row, 0)
                py_item = self.gcp_table.item(row, 1)
                v1_item = self.gcp_table.item(row, 2)
                v2_item = self.gcp_table.item(row, 3)
                if not v1_item or not v2_item:
                    continue

                v1_txt = v1_item.text().strip()
                v2_txt = v2_item.text().strip()

                if " " in v1_txt:
                    parts1 = v1_txt.split()
                    parts2 = v2_txt.split()
                    ld, lm, ls = abs(float(parts1[0])), abs(float(parts1[1])), abs(float(parts1[2]))
                    ond, onm, ons = abs(float(parts2[0])), abs(float(parts2[1])), abs(float(parts2[2]))
                    E_lam, N_lam, _, _ = convert_hayford_point_to_lambert(island, "dms", ld, lm, ls, ond, onm, ons)
                else:
                    e_val = float(v1_txt)
                    n_val = float(v2_txt)
                    E_lam, N_lam, _, _ = convert_hayford_point_to_lambert(island, "utm", e_val, n_val)

                self.gcp_table.setItem(row, 4, QtWidgets.QTableWidgetItem(f"{E_lam:.3f}"))
                self.gcp_table.setItem(row, 5, QtWidgets.QTableWidgetItem(f"{N_lam:.3f}"))
                log += f"GCP {row+1}: Pixel({px_item.text()}, {py_item.text()}) -> Lambert E={E_lam:.3f} m, N={N_lam:.3f} m\n"

                self.georef_progress_bar.setValue(row + 1)
                QtWidgets.QApplication.processEvents()

            self.refresh_markers()
            self.georef_log.setText(log)
            self.georef_progress_bar.setValue(max(1, total_rows))
            self.georef_progress_bar.setFormat("Recálculo dos GCPs Concluído! ✓")
            return True
        except Exception as e:
            self.georef_log.setText(f"Erro ao converter GCPs: {str(e)}")
            return False
        finally:
            self.btn_calc_gcps.setText("1. Recalcular Todos os GCPs para Lambert")
            self.btn_calc_gcps.setEnabled(True)
            self.btn_warp.setEnabled(True)
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QApplication.processEvents()

    def warp_image(self):
        if not self.calculate_all_gcps():
            return

        in_raster = self.input_raster_path.text().strip()
        out_raster = self.input_out_geotiff.text().strip()

        if not in_raster or not os.path.exists(in_raster):
            QtWidgets.QMessageBox.warning(self, "Aviso", "Selecione uma imagem de entrada válida!")
            return

        if not out_raster:
            QtWidgets.QMessageBox.warning(self, "Aviso", "Defina o ficheiro GeoTIFF de saída!")
            return

        try:
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            self.btn_warp.setText("⏳ A Processar Retificação (GDAL Warp)... Por favor aguarde")
            self.btn_warp.setStyleSheet("background-color: #E65100; color: white; font-weight: bold; padding: 6px;")
            self.btn_warp.setEnabled(False)
            self.btn_calc_gcps.setEnabled(False)

            self.georef_progress_bar.setVisible(True)
            self.georef_progress_bar.setRange(0, 0)
            self.georef_progress_bar.setFormat("⚙️ GDAL Warp em execução... A processar imagem GeoTIFF...")
            QtWidgets.QApplication.processEvents()

            from osgeo import gdal, osr

            gdal_gcps = []
            for row in range(self.gcp_table.rowCount()):
                px = float(self.gcp_table.item(row, 0).text())
                py = float(self.gcp_table.item(row, 1).text())
                map_x = float(self.gcp_table.item(row, 4).text())
                map_y = float(self.gcp_table.item(row, 5).text())
                gdal_gcps.append(gdal.GCP(map_x, map_y, 0.0, px, py))

            if len(gdal_gcps) < 3:
                QtWidgets.QMessageBox.warning(self, "Aviso", "São necessários pelo menos 3 GCPs para o georreferenciamento!")
                return

            algo_str = self.georef_algo_combo.currentText()
            self.georef_log.append(f"\nA iniciar retificação via GDAL Warp [{algo_str}] (Lambert EPSG:4826)...")

            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4826)

            ds_in = gdal.Open(in_raster)
            if not ds_in:
                QtWidgets.QMessageBox.critical(self, "Erro", "Não foi possível abrir a imagem raster de entrada!")
                return

            driver_vrt = gdal.GetDriverByName('VRT')
            ds_vrt = driver_vrt.CreateCopy('', ds_in)
            srs_wkt = srs.ExportToWkt()
            ds_vrt.SetGCPs(gdal_gcps, srs_wkt)

            algo_idx = self.georef_algo_combo.currentIndex()
            if algo_idx == 1:
                p_order = 2
                tps_flag = False
            elif algo_idx == 2:
                p_order = None
                tps_flag = True
            else:
                p_order = 1
                tps_flag = False

            resample_idx = self.georef_resample_combo.currentIndex()
            if resample_idx == 1:
                resample_alg = gdal.GRA_NearestNeighbour
            elif resample_idx == 2:
                resample_alg = gdal.GRA_Cubic
            elif resample_idx == 3:
                resample_alg = gdal.GRA_CubicSpline
            elif resample_idx == 4:
                resample_alg = gdal.GRA_Lanczos
            else:
                resample_alg = gdal.GRA_Bilinear

            resample_str = self.georef_resample_combo.currentText()
            self.georef_log.append(f"Reamostragem: {resample_str}")

            if tps_flag:
                warp_options = gdal.WarpOptions(
                    format='GTiff',
                    dstSRS=srs,
                    tps=True,
                    resampleAlg=resample_alg
                )
            else:
                warp_options = gdal.WarpOptions(
                    format='GTiff',
                    dstSRS=srs,
                    polynomialOrder=p_order,
                    resampleAlg=resample_alg
                )

            out_ds = gdal.Warp(out_raster, ds_vrt, options=warp_options)
            out_ds = None
            ds_vrt = None
            ds_in = None

            self.georef_log.append(f"\nSUCCESS: GeoTIFF Georreferenciado gerado em:\n{out_raster}")

            if self.check_load_qgis.isChecked() and self.iface:
                layer_name = os.path.basename(out_raster)
                rlayer = QgsRasterLayer(out_raster, layer_name)
                if rlayer.isValid():
                    QgsProject.instance().addMapLayer(rlayer)
                    self.georef_log.append(f"Camada '{layer_name}' carregada com sucesso na tela do QGIS!")
                else:
                    self.georef_log.append("Aviso: Não foi possível carregar a camada no QGIS.")

            self.georef_progress_bar.setRange(0, 100)
            self.georef_progress_bar.setValue(100)
            self.georef_progress_bar.setFormat("Georreferenciamento Concluído com Sucesso! ✓")

            QtWidgets.QMessageBox.information(self, "Sucesso", "Imagem georreferenciada e exportada com sucesso em Lambert (EPSG:4826)!")
        except Exception as e:
            self.georef_log.append(f"\nErro no GDAL Warp: {str(e)}")
            self.georef_progress_bar.setRange(0, 100)
            self.georef_progress_bar.setValue(0)
            self.georef_progress_bar.setFormat("Falha no Processamento GDAL Warp! ❌")
            QtWidgets.QMessageBox.critical(self, "Erro", f"Falha no georreferenciamento: {str(e)}")
        finally:
            self.btn_warp.setText("2. Georreferenciar Imagem (GDAL Warp)")
            self.btn_warp.setStyleSheet("background-color: #2E7D32; color: white; font-weight: bold; padding: 6px;")
            self.btn_warp.setEnabled(True)
            self.btn_calc_gcps.setEnabled(True)
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QApplication.processEvents()

    def browse_geoid_t2(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Selecionar Modelo Geoidal Raster", "", "Rasters (*.tif *.gtx *.asc *.grd)")
        if filename:
            self.input_geoid_t2.setText(filename)

    def browse_geoid_t2_camada(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Selecionar Modelo Geoidal Raster", "", "Rasters (*.tif *.gtx *.asc *.grd)")
        if filename:
            self.input_geoid_t2_camada.setText(filename)

    def on_geoid_model_ponto_changed(self, idx):
        if idx == 0:
            self.input_geoid_t2.setText("EGM2008 (Global)")
        else:
            if self.input_geoid_t2.text() == "EGM2008 (Global)":
                self.input_geoid_t2.setText("")

    def on_geoid_model_camada_changed(self, idx):
        if idx == 0:
            self.input_geoid_t2_camada.setText("EGM2008 (Global)")
        else:
            if self.input_geoid_t2_camada.text() == "EGM2008 (Global)":
                self.input_geoid_t2_camada.setText("")

    def browse_vector_v1(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Selecionar Camada Vetorial", "", "Vetores (*.shp *.gpkg *.geojson)")
        if filename:
            self.input_file_v1.setText(filename)

    def browse_vector_v2(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Selecionar Camada Vetorial", "", "Vetores (*.shp *.gpkg *.geojson)")
        if filename:
            self.input_file_v2.setText(filename)
            self.update_h_fields()

    def update_h_fields(self):
        if not hasattr(self, 'combo_h_field'):
            return
        self.combo_h_field.clear()
        self.combo_h_field.addItem("[ Sem Campo / Usar h = 0.0 m ]", None)

        layer = self.combo_vlayers_t2.currentData()
        fpath = self.input_file_v2.text().strip()
        if not layer and fpath and os.path.exists(fpath):
            try:
                layer = QgsVectorLayer(fpath, os.path.basename(fpath), "ogr")
            except Exception:
                layer = None

        if layer and layer.isValid():
            try:
                if QgsWkbTypes.hasZ(layer.wkbType()):
                    self.combo_h_field.addItem("[ Coordenada Z da Geometria 3D ]", "__GEOMETRY_Z__")
            except Exception as e_z:
                _has_z_err = str(e_z)

            best_idx = 0
            for field in layer.fields():
                fname = field.name()
                idx = self.combo_h_field.count()
                self.combo_h_field.addItem(f"Campo: {fname}", fname)
                fname_lower = fname.lower()
                if fname_lower in ["h", "h_elips", "h_elipsoidal", "altitude", "z", "elevation", "height", "cot", "cota"]:
                    best_idx = idx
                elif best_idx == 0 and any(k in fname_lower for k in ["h_el", "alt", "elev", "z"]):
                    best_idx = idx

            if best_idx > 0:
                self.combo_h_field.setCurrentIndex(best_idx)

    def refresh_vector_layers(self, combo):
        combo.clear()
        if self.iface:
            layers = QgsProject.instance().mapLayers().values()
            for lyr in layers:
                if isinstance(lyr, QgsVectorLayer) and lyr.isValid():
                    combo.addItem(lyr.name(), lyr)
        if combo == getattr(self, 'combo_vlayers_t2', None):
            self.update_h_fields()

    def update_t1_alt_fields(self):
        alt_idx = self.combo_alt_type.currentIndex()
        island = self.combo_island.currentText()
        params = BURSA_WOLF_PARAMS.get(island, {})
        def_n = params.get("default_N", 47.50)

        if alt_idx == 0:
            self.lbl_h_input.setText("Altitude Ortométrica (H em m):")
            self.lbl_n_geo.show()
            self.input_n_geo.show()
            self.input_n_geo.setText(f"{def_n:.2f}")
        else:
            self.lbl_h_input.setText("Altitude Elipsoidal (h em m):")
            self.lbl_n_geo.hide()
            self.input_n_geo.hide()

    def run_transformation_ponto(self):
        try:
            island = self.combo_island.currentText()
            mode = self.combo_mode.currentIndex()
            params = BURSA_WOLF_PARAMS[island]
            fmt_idx = self.combo_t1_fmt.currentIndex()
            alt_type = self.combo_alt_type.currentIndex()
            val_alt = float(self.input_h.text())

            if alt_type == 0:
                n_geo = float(self.input_n_geo.text())
                h = val_alt + n_geo
            else:
                n_geo = 0.0
                h = val_alt

            if fmt_idx == 0:
                E = float(self.input_easting.text())
                N = float(self.input_northing.text())
            elif fmt_idx == 1:
                lat_val = float(self.input_t1_lat_dec.text())
                lon_val = float(self.input_t1_lon_dec.text())
                if mode == 0:
                    E, N = hayford_geodetic_to_utm(lat_val * math.pi / 180.0, lon_val * math.pi / 180.0, params["utm_zone"])
                else:
                    E, N = wgs84_to_lambert(lat_val * math.pi / 180.0, lon_val * math.pi / 180.0)
            else:
                lat_val = dms_to_decimal(self.t1_lat_d.text(), self.t1_lat_m.text(), self.t1_lat_s.text(), "N")
                lon_val = dms_to_decimal(self.t1_lon_d.text(), self.t1_lon_m.text(), self.t1_lon_s.text(), "W")
                if mode == 0:
                    E, N = hayford_geodetic_to_utm(lat_val * math.pi / 180.0, lon_val * math.pi / 180.0, params["utm_zone"])
                else:
                    E, N = wgs84_to_lambert(lat_val * math.pi / 180.0, lon_val * math.pi / 180.0)

            out = f"=== RESULTADO DA TRANSFORMAÇÃO 7 PARÂMETROS (BURSA-WOLF) ===\n"
            out += f"Ilha: {island} (Fuso UTM {params['utm_zone']}N)\n\n"

            if mode == 0:
                lat_h, lon_h = utm_to_hayford_geodetic(E, N, params["utm_zone"])
                X_h, Y_h, Z_h = geodetic_to_cartesian(lat_h, lon_h, h, A_HAYFORD, E2_HAYFORD)
                X_w, Y_w, Z_w = apply_bursa_wolf_direct(X_h, Y_h, Z_h, params)
                lat_w, lon_w, h_w = cartesian_to_geodetic(X_w, Y_w, Z_w, A_WGS84, E2_WGS84)
                E_lam, N_lam = wgs84_to_lambert(lat_w, lon_w)

                out += f"Entrada (Hayford-UTM): Easting = {E:.3f} m, Northing = {N:.3f} m, h = {h:.3f} m\n"
                out += f"Geodésicas Hayford: Lat = {lat_h*180/math.pi:.6f}°, Lon = {lon_h*180/math.pi:.6f}°\n"
                out += f"Cartesianas 3D ITRF96: X = {X_w:.3f} m, Y = {Y_w:.3f} m, Z = {Z_w:.3f} m\n"
                out += f"--> SAÍDA LAMBERT CABO VERDE (EPSG:4826):\n"
                out += f"    Easting  (X): {E_lam:.3f} m\n"
                out += f"    Northing (Y): {N_lam:.3f} m\n"
                out += f"    Latitude WGS84: {lat_w*180/math.pi:.7f}°\n"
                out += f"    Longitude WGS84: {lon_w*180/math.pi:.7f}°\n"
                out += f"    Altitude Elipsoidal WGS84 (h): {h_w:.3f} m\n"
            else:
                lat_w, lon_w = lambert_to_wgs84(E, N)
                X_w, Y_w, Z_w = geodetic_to_cartesian(lat_w, lon_w, h, A_WGS84, E2_WGS84)
                X_h, Y_h, Z_h = apply_bursa_wolf_inverse(X_w, Y_w, Z_w, params)
                lat_h, lon_h, h_h = cartesian_to_geodetic(X_h, Y_h, Z_h, A_HAYFORD, E2_HAYFORD)
                E_utm, N_utm = hayford_geodetic_to_utm(lat_h, lon_h, params["utm_zone"])

                out += f"Entrada (Lambert EPSG:4826): Easting = {E:.3f} m, Northing = {N:.3f} m, h = {h:.3f} m\n"
                out += f"Geodésicas WGS84: Lat = {lat_w*180/math.pi:.7f}°, Lon = {lon_w*180/math.pi:.7f}°\n"
                crs_name_ponto = f"Hayford 1924 / UTM Fuso {params['utm_zone']}N - {island}"
                out += f"--> SAÍDA HAYFORD-UTM ({crs_name_ponto}):\n"
                out += f"    Easting  (E): {E_utm:.3f} m\n"
                out += f"    Northing (N): {N_utm:.3f} m\n"
                out += f"    Latitude Hayford: {lat_h*180/math.pi:.6f}°\n"
                out += f"    Longitude Hayford: {lon_h*180/math.pi:.6f}°\n"
                out += f"    Altitude Elipsoidal Hayford (h): {h_h:.3f} m\n"

            s_xy = params.get("sigma_xy", 0.22)
            s_z = params.get("sigma_z", 0.35)
            s_3d = params.get("sigma_3d", 0.41)

            out += "\n==================================================\n"
            out += "ESTIMATIVA DE INCERTEZA POSICIONAL (RMS - 95% Confiança):\n"
            out += f"  Incerteza Planimétrica  (σ_XY) : ± {s_xy:.3f} m\n"
            out += f"  Incerteza Altimétrica   (σ_Z)  : ± {s_z:.3f} m\n"
            out += f"  Incerteza Tridimensional (σ_3D) : ± {s_3d:.3f} m\n"
            out += f"  Nota: Resíduos do ajustamento histórico do IGP (2009) para {island}.\n"
            out += "==================================================\n\n"
            self.result_text_ponto.setText(out)
        except Exception as e:
            self.result_text_ponto.setText(f"Erro na transformação: {str(e)}")

    def run_transformation_camada(self):
        try:
            layer = self.combo_vlayers_t1.currentData()
            fpath = self.input_file_v1.text().strip()
            if not layer and fpath and os.path.exists(fpath):
                layer = QgsVectorLayer(fpath, os.path.basename(fpath), "ogr")

            if not layer or not layer.isValid():
                QtWidgets.QMessageBox.warning(self, "Aviso", "Selecione uma camada vetorial válida!")
                return

            island = self.combo_island_v1.currentText()
            mode = self.combo_mode_v1.currentIndex()
            params = BURSA_WOLF_PARAMS[island]
            zone = params["utm_zone"]

            if mode == 0:
                target_crs = QgsCoordinateReferenceSystem("EPSG:4826")
                crs_desc = "EPSG:4826 (Lambert Cabo Verde)"
                layer_name_out = f"{layer.name()}_lambert_epsg4826"
            else:
                dx = params["dx"]
                dy = params["dy"]
                dz = params["dz"]
                rx = params["rx_sec"]
                ry = params["ry_sec"]
                rz = params["rz_sec"]
                ds = params["ds_ppm"]
                crs_name = f"Hayford 1924 / UTM Fuso {zone}N - {island}"
                cm = -27 if zone == 26 else -21
                wkt_str = f'PROJCS["{crs_name}",GEOGCS["Hayford 1924",DATUM["International 1924",SPHEROID["International 1924",6378388,297],TOWGS84[{dx},{dy},{dz},{rx},{ry},{rz},{ds}]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",{cm}],PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],PARAMETER["false_northing",0],UNIT["metre",1]]'
                proj_str = f"+proj=utm +zone={zone} +ellps=intl +towgs84={dx},{dy},{dz},{rx},{ry},{rz},{ds} +units=m +no_defs"
                target_crs = QgsCoordinateReferenceSystem()
                target_crs.createFromWkt(wkt_str)
                if not target_crs.isValid():
                    target_crs.createFromUserInput(proj_str)
                crs_desc = crs_name
                layer_name_out = f"{layer.name()}_hayford_utm{zone}n"

            geom_type = QgsWkbTypes.displayString(layer.wkbType())
            out_layer = QgsVectorLayer(geom_type, layer_name_out, "memory")
            out_layer.setCrs(target_crs)
            out_dp = out_layer.dataProvider()
            out_dp.addAttributes(layer.fields())
            out_layer.updateFields()

            out_features = []
            for feat in layer.getFeatures():
                geom = feat.geometry()
                new_geom = self.transform_geometry_bursa_wolf(geom, island, mode)
                new_feat = QgsFeature(feat)
                new_feat.setGeometry(new_geom)
                out_features.append(new_feat)

            out_dp.addFeatures(out_features)
            out_layer.updateExtents()

            if self.iface:
                QgsProject.instance().addMapLayer(out_layer)

            s_xy = params.get("sigma_xy", 0.22)
            s_z = params.get("sigma_z", 0.35)
            s_3d = params.get("sigma_3d", 0.41)
            msg = msg = f"SUCCESS: Camada '{layer.name()}' transformada com sucesso (Bursa-Wolf {island})!\nTotal de feições: {len(out_features)}\nCRS Atribuído: {crs_desc}\n\nIncerteza Posicional Estimada (RMS 95%):\n  σ_XY: ±{s_xy:.3f} m | σ_Z: ±{s_z:.3f} m | σ_3D: ±{s_3d:.3f} m"
            self.result_text_camada.setText(msg)
            QtWidgets.QMessageBox.information(self, "Sucesso", msg)
        except Exception as e:
            self.result_text_camada.setText(f"Erro na conversão da camada: {str(e)}")

    def transform_geometry_bursa_wolf(self, geom, island, mode):
        params = BURSA_WOLF_PARAMS[island]
        zone = params["utm_zone"]

        def transform_point_coords(x, y, z=0.0):
            if mode == 0:
                lat_h, lon_h = utm_to_hayford_geodetic(x, y, zone)
                X_h, Y_h, Z_h = geodetic_to_cartesian(lat_h, lon_h, z, A_HAYFORD, E2_HAYFORD)
                X_w, Y_w, Z_w = apply_bursa_wolf_direct(X_h, Y_h, Z_h, params)
                lat_w, lon_w, h_w = cartesian_to_geodetic(X_w, Y_w, Z_w, A_WGS84, E2_WGS84)
                E_lam, N_lam = wgs84_to_lambert(lat_w, lon_w)
                return E_lam, N_lam, h_w
            else:
                lat_w, lon_w = lambert_to_wgs84(x, y)
                X_w, Y_w, Z_w = geodetic_to_cartesian(lat_w, lon_w, z, A_WGS84, E2_WGS84)
                X_h, Y_h, Z_h = apply_bursa_wolf_inverse(X_w, Y_w, Z_w, params)
                lat_h, lon_h, h_h = cartesian_to_geodetic(X_h, Y_h, Z_h, A_HAYFORD, E2_HAYFORD)
                E_utm, N_utm = hayford_geodetic_to_utm(lat_h, lon_h, zone)
                return E_utm, N_utm, h_h

        if geom.type() == QgsWkbTypes.GeometryType.PointGeometry:
            if geom.isMultipart():
                pts = geom.asMultiPoint()
                new_pts = [QgsPointXY(*transform_point_coords(pt.x(), pt.y())[:2]) for pt in pts]
                return QgsGeometry.fromMultiPointXY(new_pts)
            else:
                pt = geom.asPoint()
                nx, ny, _ = transform_point_coords(pt.x(), pt.y())
                return QgsGeometry.fromPointXY(QgsPointXY(nx, ny))

        elif geom.type() == QgsWkbTypes.GeometryType.LineGeometry:
            if geom.isMultipart():
                lines = geom.asMultiPolyline()
                new_lines = []
                for line in lines:
                    new_line = [QgsPointXY(*transform_point_coords(pt.x(), pt.y())[:2]) for pt in line]
                    new_lines.append(new_line)
                return QgsGeometry.fromMultiPolylineXY(new_lines)
            else:
                line = geom.asPolyline()
                new_line = [QgsPointXY(*transform_point_coords(pt.x(), pt.y())[:2]) for pt in line]
                return QgsGeometry.fromPolylineXY(new_line)

        elif geom.type() == QgsWkbTypes.GeometryType.PolygonGeometry:
            if geom.isMultipart():
                polys = geom.asMultiPolygon()
                new_polys = []
                for poly in polys:
                    new_poly = []
                    for ring in poly:
                        new_ring = [QgsPointXY(*transform_point_coords(pt.x(), pt.y())[:2]) for pt in ring]
                        new_poly.append(new_ring)
                    new_polys.append(new_poly)
                return QgsGeometry.fromMultiPolygonXY(new_polys)
            else:
                poly = geom.asPolygon()
                new_poly = []
                for ring in poly:
                    new_ring = [QgsPointXY(*transform_point_coords(pt.x(), pt.y())[:2]) for pt in ring]
                    new_poly.append(new_ring)
                return QgsGeometry.fromPolygonXY(new_poly)
        return geom

    def run_ortometrica_ponto(self):
        try:
            mode_alt = self.combo_orto_mode_ponto.currentIndex() # 0: h->H, 1: H->h
            crs_idx = self.combo_orto_crs_ponto.currentIndex()
            alt_in = float(self.input_wgs_h.text())
            geoid_path = self.input_geoid_t2.text().strip()
            island = self.combo_orto_island_ponto.currentText()
            params = BURSA_WOLF_PARAMS.get(island, {})

            out = "=== CÁLCULO E CONVERSÃO ALTIMÉTRICA (CV-GEOTRANSFORM) ===\n"
            if mode_alt == 0:
                out += "Modo: Direto (Altitude Elipsoidal h -> Altitude Ortométrica H)\n"
            else:
                out += "Modo: Inverso (Altitude Ortométrica H -> Altitude Elipsoidal h)\n"

            if crs_idx == 0: # WGS84 Geográficas
                lat_w = float(self.input_wgs_lat.text())
                lon_w = float(self.input_wgs_lon.text())
                lat_w_rad = lat_w * math.pi / 180.0
                lon_w_rad = lon_w * math.pi / 180.0
                E_lam, N_lam = wgs84_to_lambert(lat_w_rad, lon_w_rad)
                out += f"Sistema de Entrada: WGS84 Geográficas (EPSG:4326)\n"
                out += f"Coordenadas de Entrada: Lat = {lat_w:.7f}°, Lon = {lon_w:.7f}°\n"
                out += f"Equivalente Lambert EPSG:4826: Easting = {E_lam:.3f} m, Northing = {N_lam:.3f} m\n"

            elif crs_idx == 1: # Lambert
                E_lam = float(self.input_lambert_e.text())
                N_lam = float(self.input_lambert_n.text())
                lat_w_rad, lon_w_rad = lambert_to_wgs84(E_lam, N_lam)
                lat_w = lat_w_rad * 180.0 / math.pi
                lon_w = lon_w_rad * 180.0 / math.pi
                out += f"Sistema de Entrada: Lambert Cabo Verde (EPSG:4826)\n"
                out += f"Coordenadas de Entrada: Easting = {E_lam:.3f} m, Northing = {N_lam:.3f} m\n"
                out += f"Equivalente WGS84 Convertido: Lat = {lat_w:.7f}°, Lon = {lon_w:.7f}°\n"

            elif crs_idx in (2, 3): # Hayford UTM 26N / 27N
                zone = 26 if crs_idx == 2 else 27
                E_utm = float(self.input_h_utm_e.text())
                N_utm = float(self.input_h_utm_n.text())
                lat_h_rad, lon_h_rad = utm_to_hayford_geodetic(E_utm, N_utm, zone)
                X_h, Y_h, Z_h = geodetic_to_cartesian(lat_h_rad, lon_h_rad, alt_in, A_HAYFORD, E2_HAYFORD)
                X_w, Y_w, Z_w = apply_bursa_wolf_direct(X_h, Y_h, Z_h, params)
                lat_w_rad, lon_w_rad, h_w = cartesian_to_geodetic(X_w, Y_w, Z_w, A_WGS84, E2_WGS84)
                lat_w = lat_w_rad * 180.0 / math.pi
                lon_w = lon_w_rad * 180.0 / math.pi
                E_lam, N_lam = wgs84_to_lambert(lat_w_rad, lon_w_rad)

                out += f"Sistema de Entrada: Hayford UTM Fuso {zone}N ({island})\n"
                out += f"Coordenadas de Entrada: Easting = {E_utm:.3f} m, Northing = {N_utm:.3f} m\n"
                out += f"Equivalente Hayford Geodésicas: Lat = {lat_h_rad*180/math.pi:.6f}°, Lon = {lon_h_rad*180/math.pi:.6f}°\n"
                out += f"Equivalente WGS84 (Bursa-Wolf): Lat = {lat_w:.7f}°, Lon = {lon_w:.7f}°\n"
                out += f"Equivalente Lambert EPSG:4826: Easting = {E_lam:.3f} m, Northing = {N_lam:.3f} m\n"

            else: # Hayford Geográficas
                lat_h = float(self.input_h_geo_lat.text())
                lon_h = float(self.input_h_geo_lon.text())
                lat_h_rad = lat_h * math.pi / 180.0
                lon_h_rad = lon_h * math.pi / 180.0
                zone = params.get("utm_zone", 26)
                E_utm, N_utm = hayford_geodetic_to_utm(lat_h_rad, lon_h_rad, zone)
                X_h, Y_h, Z_h = geodetic_to_cartesian(lat_h_rad, lon_h_rad, alt_in, A_HAYFORD, E2_HAYFORD)
                X_w, Y_w, Z_w = apply_bursa_wolf_direct(X_h, Y_h, Z_h, params)
                lat_w_rad, lon_w_rad, h_w = cartesian_to_geodetic(X_w, Y_w, Z_w, A_WGS84, E2_WGS84)
                lat_w = lat_w_rad * 180.0 / math.pi
                lon_w = lon_w_rad * 180.0 / math.pi
                E_lam, N_lam = wgs84_to_lambert(lat_w_rad, lon_w_rad)

                out += f"Sistema de Entrada: Hayford Geográficas ({island})\n"
                out += f"Coordenadas de Entrada: Lat = {lat_h:.6f}°, Lon = {lon_h:.6f}°\n"
                out += f"Equivalente Hayford UTM Fuso {zone}N: Easting = {E_utm:.3f} m, Northing = {N_utm:.3f} m\n"
                out += f"Equivalente WGS84 (Bursa-Wolf): Lat = {lat_w:.7f}°, Lon = {lon_w:.7f}°\n"
                out += f"Equivalente Lambert EPSG:4826: Easting = {E_lam:.3f} m, Northing = {N_lam:.3f} m\n"

            out += "\n--- AMOSTRAGEM DO MODELO GEOIDAL ---\n"
            N_val = 0.0
            model_name = ""
            if geoid_path and os.path.exists(geoid_path):
                N_val, model_name = sample_geoid_raster_with_name(geoid_path, lat_w, lon_w)
                out += f"Ficheiro Raster Geoidal: {model_name}\n"
                out += f"Ondulação Geoidal Amostrada do Raster (N): {N_val:.4f} m\n"

            if N_val == 0.0:
                try:
                    N_val = float(self.input_manual_n_t2.text())
                    out += f"Ondulação Geoidal Adotada (Manual / Média da Ilha): {N_val:.4f} m\n"
                except Exception:
                    N_val = params.get("default_N", 47.50)
                    out += f"Ondulação Geoidal Padrão da Ilha (N): {N_val:.4f} m\n"

            out += "\n==================================================\n"
            out += "RESULTADO DA CONVERSÃO ALTIMÉTRICA:\n"
            if mode_alt == 0: # h -> H
                h_val = alt_in
                H_val = h_val - N_val
                out += f"  Entrada (Altitude Elipsoidal GNSS - h) : {h_val:.3f} m\n"
                out += f"  Ondulação Geoidal Aplicada (N)          : {N_val:.4f} m\n"
                out += f"  --> ALTITUDE ORTOMÉTRICA CALCULADA (H)  : {H_val:.3f} m\n"
            else: # H -> h
                H_val = alt_in
                h_val = H_val + N_val
                out += f"  Entrada (Altitude Ortométrica - H)     : {H_val:.3f} m\n"
                out += f"  Ondulação Geoidal Aplicada (N)          : {N_val:.4f} m\n"
                out += f"  --> ALTITUDE ELIPSOIDAL CALCULADA (h)   : {h_val:.3f} m\n"
            out += "==================================================\n"

            self.result_orto_ponto.setText(out)
        except Exception as e:
            self.result_orto_ponto.setText(f"Erro no cálculo altimétrico: {str(e)}")

    def run_ortometrica_camada(self):
        try:
            layer = self.combo_vlayers_t2.currentData()
            fpath = self.input_file_v2.text().strip()
            if not layer and fpath and os.path.exists(fpath):
                layer = QgsVectorLayer(fpath, os.path.basename(fpath), "ogr")

            if not layer or not layer.isValid():
                QtWidgets.QMessageBox.warning(self, "Aviso", "Selecione uma camada vetorial válida!")
                return

            geoid_path = self.input_geoid_t2_camada.text().strip()
            mode_op = self.combo_orto_op.currentIndex()
            mode_alt = self.combo_orto_mode_camada.currentIndex()

            out_layer = QgsVectorLayer(f"{QgsWkbTypes.displayString(layer.wkbType())}?crs=EPSG:4326", f"{layer.name()}_altimetria", "memory")
            out_dp = out_layer.dataProvider()

            fields = layer.fields()
            if mode_op == 0:
                fields.append(QgsField("N_geoid", QVariant.Double))
                fields.append(QgsField("H_orto", QVariant.Double))
                fields.append(QgsField("h_elips", QVariant.Double))

            out_dp.addAttributes(fields)
            out_layer.updateFields()

            crs_src = layer.crs()
            crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            transform_to_wgs = QgsCoordinateTransform(crs_src, crs_wgs84, QgsProject.instance())

            h_field_name = self.combo_h_field.currentData() if hasattr(self, 'combo_h_field') else None
            manual_n = 47.50
            try:
                manual_n = float(self.input_manual_n_camada.text())
            except Exception as e_n:
                manual_n = 47.50

            out_features = []
            for feat in layer.getFeatures():
                geom = feat.geometry()
                pt_wgs = transform_to_wgs.transform(geom.centroid().asPoint())
                N_val, _ = sample_geoid_raster_with_name(geoid_path, pt_wgs.y(), pt_wgs.x())
                if N_val == 0.0:
                    N_val = manual_n

                alt_in = 0.0
                if h_field_name == "__GEOMETRY_Z__":
                    try:
                        if geom and not geom.isEmpty():
                            p = geom.centroid().asPoint()
                            if hasattr(p, 'z') and not math.isnan(p.z()):
                                alt_in = float(p.z())
                    except Exception:
                        alt_in = 0.0
                elif h_field_name:
                    try:
                        val = feat[h_field_name]
                        if val is not None and str(val).strip() != "":
                            alt_in = float(val)
                    except Exception:
                        try:
                            val = feat.attribute(h_field_name)
                            if val is not None and str(val).strip() != "":
                                alt_in = float(val)
                        except Exception:
                            alt_in = 0.0

                if mode_alt == 0: # h -> H
                    h_val = alt_in
                    H_val = h_val - N_val
                else: # H -> h
                    H_val = alt_in
                    h_val = H_val + N_val

                new_feat = QgsFeature(feat)
                attrs = feat.attributes()
                if mode_op == 0:
                    attrs.extend([round(N_val, 4), round(H_val, 3), round(h_val, 3)])
                    new_feat.setAttributes(attrs)

                out_features.append(new_feat)

            out_dp.addFeatures(out_features)
            out_layer.updateExtents()

            if self.iface:
                QgsProject.instance().addMapLayer(out_layer)

            msg = f"SUCCESS: Conversão altimétrica concluída para {len(out_features)} feições!"
            self.result_orto_camada.setText(msg)
            QtWidgets.QMessageBox.information(self, "Sucesso", msg)
        except Exception as e:
            self.result_orto_camada.setText(f"Erro ao processar camada: {str(e)}")

    def setup_epoch_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_epoch)
        self.tabs_t4 = QtWidgets.QTabWidget()
        self.tabs_t4.setMovable(True)
        layout.addWidget(self.tabs_t4)

        self.sub_t4_ponto = QtWidgets.QWidget()
        self.setup_sub_t4_ponto()
        self.tabs_t4.addTab(self.sub_t4_ponto, "4.1 Ponto Único")

        self.sub_t4_camada = QtWidgets.QWidget()
        self.setup_sub_t4_camada()
        self.tabs_t4.addTab(self.sub_t4_camada, "4.2 Camada Vetorial")

    def setup_sub_t4_ponto(self):
        layout = QtWidgets.QVBoxLayout(self.sub_t4_ponto)
        form = QtWidgets.QFormLayout()

        self.combo_epoch_crs_ponto = QtWidgets.QComboBox()
        self.combo_epoch_crs_ponto.addItems([
            "Lambert Cabo Verde (EPSG:4826 - Easting X / Northing Y em m)",
            "Geográficas WGS84 / ITRF (EPSG:4326 - Latitude / Longitude em °)"
        ])
        self.combo_epoch_crs_ponto.currentIndexChanged.connect(self.toggle_epoch_crs_ponto)
        form.addRow("Sistema de Coordenadas de Entrada:", self.combo_epoch_crs_ponto)

        layout.addLayout(form)

        # Container 1: Lambert Métrico
        self.widget_epoch_lambert = QtWidgets.QWidget()
        lay_lam = QtWidgets.QFormLayout(self.widget_epoch_lambert)
        lay_lam.setContentsMargins(0, 0, 0, 0)
        self.input_epoch_e = QtWidgets.QLineEdit("205276.357")
        self.input_epoch_n = QtWidgets.QLineEdit("36280.487")
        lay_lam.addRow("Easting Lambert E (m):", self.input_epoch_e)
        lay_lam.addRow("Northing Lambert N (m):", self.input_epoch_n)
        layout.addWidget(self.widget_epoch_lambert)

        # Container 2: Geográficas WGS84
        self.widget_epoch_wgs = QtWidgets.QWidget()
        lay_wgs = QtWidgets.QFormLayout(self.widget_epoch_wgs)
        lay_wgs.setContentsMargins(0, 0, 0, 0)
        self.input_epoch_lat = QtWidgets.QLineEdit("14.999379")
        self.input_epoch_lon = QtWidgets.QLineEdit("-23.593787")
        lay_wgs.addRow("Latitude WGS84 (Graus Decimais °):", self.input_epoch_lat)
        lay_wgs.addRow("Longitude WGS84 (Graus Decimais °):", self.input_epoch_lon)
        layout.addWidget(self.widget_epoch_wgs)
        self.widget_epoch_wgs.hide()

        form_epochs = QtWidgets.QFormLayout()

        self.input_obs_epoch = QtWidgets.QLineEdit("2026.85")
        form_epochs.addRow("Época da Medição GNSS (ex.: 2026.85):", self.input_obs_epoch)

        self.input_ref_epoch = QtWidgets.QLineEdit("1996.0")
        form_epochs.addRow("Época de Destino (Oficial ITRF96):", self.input_ref_epoch)

        self.input_ve = QtWidgets.QLineEdit("17.5")
        form_epochs.addRow("Velocidade Este (Ve em mm/ano):", self.input_ve)

        self.input_vn = QtWidgets.QLineEdit("15.2")
        form_epochs.addRow("Velocidade Norte (Vn em mm/ano):", self.input_vn)

        layout.addLayout(form_epochs)

        btn_calc = QtWidgets.QPushButton("Executar Redução de Época para ITRF96 (1996.0)")
        btn_calc.setStyleSheet("background-color: #1565C0; color: white; font-weight: bold; padding: 6px;")
        btn_calc.clicked.connect(self.run_epoch_ponto)
        layout.addWidget(btn_calc)

        self.result_epoch_ponto = QtWidgets.QTextEdit()
        self.result_epoch_ponto.setReadOnly(True)
        layout.addWidget(self.result_epoch_ponto)

    def toggle_epoch_crs_ponto(self, idx):
        if idx == 0:
            self.widget_epoch_lambert.show()
            self.widget_epoch_wgs.hide()
        else:
            self.widget_epoch_lambert.hide()
            self.widget_epoch_wgs.show()

    def run_epoch_ponto(self):
        try:
            crs_idx = self.combo_epoch_crs_ponto.currentIndex()
            if crs_idx == 0:
                v1 = float(self.input_epoch_e.text())
                v2 = float(self.input_epoch_n.text())
            else:
                v1 = float(self.input_epoch_lat.text())
                v2 = float(self.input_epoch_lon.text())
            t_obs = float(self.input_obs_epoch.text())
            t_ref = float(self.input_ref_epoch.text())
            ve = float(self.input_ve.text())
            vn = float(self.input_vn.text())

            out = "=== REDUÇÃO DE ÉPOCA ITRF96 (DERIVA TECTÓNICA) ===\n"
            out += f"Época da Medição (t): {t_obs:.2f}\n"
            out += f"Época de Referência Oficial (t0): {t_ref:.1f}\n"
            out += f"Intervalo de Tempo (Δt): {t_obs - t_ref:.2f} anos\n"
            out += f"Velocidades Adotadas: Ve = {ve:.1f} mm/ano, Vn = {vn:.1f} mm/ano\n\n"

            if crs_idx == 0:
                E_ref, N_ref, dt, dE, dN, dist = apply_epoch_reduction_planimetric(v1, v2, t_obs, t_ref, ve, vn)
                out += f"Coordenada Entrada Lambert (Época {t_obs:.2f}): Easting = {v1:.3f} m, Northing = {v2:.3f} m\n"
                out += f"Vetor de Deslocamento Tectónico:\n"
                out += f"  ΔEasting  : {dE:+.3f} m\n"
                out += f"  ΔNorthing : {dN:+.3f} m\n"
                out += f"  Deslocamento Total Acumulado : {dist:.3f} m ({dist*100:.1f} cm)\n\n"
                out += f"--> COORDENADA REDUZIDA PARA ITRF96 (ÉPOCA {t_ref:.1f}):\n"
                out += f"    Easting Lambert  (X): {E_ref:.3f} m\n"
                out += f"    Northing Lambert (Y): {N_ref:.3f} m\n"
            else:
                lat_ref, lon_ref, dt, shift_e, shift_n, dist = apply_epoch_reduction_geodetic(v1, v2, 0.0, t_obs, t_ref, ve, vn)
                out += f"Coordenada Entrada WGS84 (Época {t_obs:.2f}): Lat = {v1:.7f}°, Lon = {v2:.7f}°\n"
                out += f"Deslocamento Tectónico Acumulado: {dist:.3f} m ({dist*100:.1f} cm)\n\n"
                out += f"--> COORDENADA REDUZIDA PARA ITRF96 (ÉPOCA {t_ref:.1f}):\n"
                out += f"    Latitude ITRF96  : {lat_ref:.7f}°\n"
                out += f"    Longitude ITRF96 : {lon_ref:.7f}°\n"

            self.result_epoch_ponto.setText(out)
        except Exception as e:
            self.result_epoch_ponto.setText(f"Erro no cálculo de redução de época: {str(e)}")

    def setup_sub_t4_camada(self):
        layout = QtWidgets.QVBoxLayout(self.sub_t4_camada)
        form = QtWidgets.QFormLayout()

        v_box = QtWidgets.QHBoxLayout()
        self.combo_vlayers_t4 = QtWidgets.QComboBox()
        self.refresh_vector_layers(self.combo_vlayers_t4)
        btn_ref_v4 = QtWidgets.QPushButton("Atualizar")
        btn_ref_v4.clicked.connect(lambda: self.refresh_vector_layers(self.combo_vlayers_t4))
        v_box.addWidget(self.combo_vlayers_t4)
        v_box.addWidget(btn_ref_v4)
        form.addRow("Camada Vetorial no QGIS:", v_box)

        f_box = QtWidgets.QHBoxLayout()
        self.input_file_v4 = QtWidgets.QLineEdit()
        self.input_file_v4.setPlaceholderText("Ou selecione um ficheiro Shapefile / GeoPackage (.shp / .gpkg)")
        btn_b_v4 = QtWidgets.QPushButton("Procurar...")
        btn_b_v4.clicked.connect(self.browse_vector_v4)
        f_box.addWidget(self.input_file_v4)
        f_box.addWidget(btn_b_v4)
        form.addRow("Ficheiro de Entrada:", f_box)

        self.input_obs_epoch_c = QtWidgets.QLineEdit("2026.85")
        form.addRow("Época da Medição (t):", self.input_obs_epoch_c)

        self.input_ref_epoch_c = QtWidgets.QLineEdit("1996.0")
        form.addRow("Época de Destino ITRF96 (t0):", self.input_ref_epoch_c)

        self.input_ve_c = QtWidgets.QLineEdit("17.5")
        form.addRow("Velocidade Este (Ve mm/ano):", self.input_ve_c)

        self.input_vn_c = QtWidgets.QLineEdit("15.2")
        form.addRow("Velocidade Norte (Vn mm/ano):", self.input_vn_c)

        layout.addLayout(form)

        btn_run_v4 = QtWidgets.QPushButton("Reduzir Época de Toda a Camada Vetorial (para ITRF96 1996.0)")
        btn_run_v4.setStyleSheet("background-color: #1565C0; color: white; font-weight: bold; padding: 6px;")
        btn_run_v4.clicked.connect(self.run_epoch_camada)
        layout.addWidget(btn_run_v4)

        self.result_epoch_camada = QtWidgets.QTextEdit()
        self.result_epoch_camada.setReadOnly(True)
        layout.addWidget(self.result_epoch_camada)

    def browse_vector_v4(self):
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Selecionar Camada Vetorial", "", "Vetores (*.shp *.gpkg *.geojson)")
        if filename:
            self.input_file_v4.setText(filename)

    def run_epoch_camada(self):
        try:
            layer = self.combo_vlayers_t4.currentData()
            fpath = self.input_file_v4.text().strip()
            if not layer and fpath and os.path.exists(fpath):
                layer = QgsVectorLayer(fpath, os.path.basename(fpath), "ogr")

            if not layer or not layer.isValid():
                QtWidgets.QMessageBox.warning(self, "Aviso", "Selecione uma camada vetorial válida!")
                return

            t_obs = float(self.input_obs_epoch_c.text())
            t_ref = float(self.input_ref_epoch_c.text())
            ve = float(self.input_ve_c.text())
            vn = float(self.input_vn_c.text())

            dt = t_obs - t_ref
            ve_m = ve / 1000.0
            vn_m = vn / 1000.0
            dE = -ve_m * dt
            dN = -vn_m * dt
            shift_dist = math.sqrt(dE**2 + dN**2)

            layer_name_out = f"{layer.name()}_itrf96_1996"
            geom_type = QgsWkbTypes.displayString(layer.wkbType())
            out_layer = QgsVectorLayer(geom_type, layer_name_out, "memory")
            out_layer.setCrs(layer.crs())
            out_dp = out_layer.dataProvider()
            out_dp.addAttributes(layer.fields())
            out_layer.updateFields()

            out_features = []
            for feat in layer.getFeatures():
                geom = feat.geometry()
                new_geom = QgsGeometry(geom)
                new_geom.translate(dE, dN)
                new_feat = QgsFeature(feat)
                new_feat.setGeometry(new_geom)
                out_features.append(new_feat)

            out_dp.addFeatures(out_features)
            out_layer.updateExtents()

            if self.iface:
                QgsProject.instance().addMapLayer(out_layer)

            msg = f"SUCCESS: Camada '{layer.name()}' reduzida para ITRF96 (1996.0)!\nTotal de feições: {len(out_features)}\nIntervalo de Época: {dt:.2f} anos ({t_obs:.2f} -> {t_ref:.1f})\nDeslocamento Tectónico Aplicado: ΔE = {dE:+.3f} m, ΔN = {dN:+.3f} m (Total: {shift_dist:.3f} m)"
            self.result_epoch_camada.setText(msg)
            QtWidgets.QMessageBox.information(self, "Sucesso", msg)
        except Exception as e:
            self.result_epoch_camada.setText(f"Erro na redução de época da camada: {str(e)}")
