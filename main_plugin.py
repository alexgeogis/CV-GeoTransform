# -*- coding: utf-8 -*-
import os
from qgis.PyQt import QtWidgets, QtGui
from .gui_dialog import CVGeoTransformDialog

class CVGeoTransformPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        if not os.path.exists(icon_path):
            icon = QtGui.QIcon()
        else:
            icon = QtGui.QIcon(icon_path)

        self.action = QtWidgets.QAction(icon, "CV-GeoTransform (Ferramentas Geodésicas CV)", self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        self.iface.addPluginToVectorMenu("CV-GeoTransform", self.action)
        self.iface.addVectorToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginVectorMenu("CV-GeoTransform", self.action)
        self.iface.removeVectorToolBarIcon(self.action)

    def run(self):
        if not self.dialog:
            self.dialog = CVGeoTransformDialog(self.iface.mainWindow(), self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
