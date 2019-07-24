"""
Module for working with large geographical areas
"""

import os
import itertools
from abc import ABC, abstractmethod
import json

import numpy as np
import shapely.ops
from shapely.geometry import Polygon, MultiPolygon, shape, GeometryCollection

from .geometry import BBox, BBoxCollection, BaseGeometry, Geometry
from .constants import CRS, DataSource
from .geo_utils import transform_point
from .ogc import WebFeatureService


class AreaSplitter(ABC):
    """ Abstract class for splitter classes. It implements common methods used for splitting large area into smaller
    parts.

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param reduce_bbox_sizes: If `True` it will reduce the sizes of bounding boxes so that they will tightly fit the
        given geometry in `shape_list`.
    :type reduce_bbox_sizes: bool
    """
    def __init__(self, shape_list, crs, reduce_bbox_sizes=False):
        self.crs = CRS(crs)
        self._parse_shape_list(shape_list, self.crs)
        self.shape_list = shape_list
        self.area_shape = self._join_shape_list(shape_list)
        self.reduce_bbox_sizes = reduce_bbox_sizes

        self.area_bbox = self.get_area_bbox()
        self.bbox_list = None
        self.info_list = None

    @staticmethod
    def _parse_shape_list(shape_list, crs):
        """ Checks if the given list of shapes is in correct format and parses geometry objects

        :param shape_list: The parameter `shape_list` from class initialization
        :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
        :raises: ValueError
        """
        if not isinstance(shape_list, list):
            raise ValueError('Splitter must be initialized with a list of shapes')

        return [AreaSplitter._parse_shape(shape, crs) for shape in shape_list]

    @staticmethod
    def _parse_shape(shape, crs):
        """ Helper method for parsing input shapes
        """
        if isinstance(shape, (Polygon, MultiPolygon)):
            return shape
        if isinstance(shape, BaseGeometry):
            return shape.transform(crs).geometry
        raise ValueError('The list of shapes must contain shapes of types {}, {} or subtype of '
                         '{}'.format(type(Polygon), type(MultiPolygon), type(BaseGeometry)))

    @staticmethod
    def _join_shape_list(shape_list):
        """ Joins a list of shapes together into one shape

        :param shape_list: A list of geometrical shapes describing the area of interest
        :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
        :return: A multipolygon which is a union of shapes in given list
        :rtype: shapely.geometry.multipolygon.MultiPolygon
        """
        return shapely.ops.cascaded_union(shape_list)

    @abstractmethod
    def _make_split(self):
        """ The abstract method where the splitting will happen
        """
        raise NotImplementedError

    def get_bbox_list(self, crs=None, buffer=None, reduce_bbox_sizes=None):
        """ Returns a list of bounding boxes that are the result of the split

        :param crs: Coordinate reference system in which the bounding boxes should be returned. If `None` the CRS will
            be the default CRS of the splitter.
        :type crs: CRS or None
        :param buffer: A percentage of each BBox size increase. This will cause neighbouring bounding boxes to overlap.
        :type buffer: float or None
        :param reduce_bbox_sizes: If `True` it will reduce the sizes of bounding boxes so that they will tightly
            fit the given geometry in `shape_list`. This overrides the same parameter from constructor
        :type reduce_bbox_sizes: bool
        :return: List of bounding boxes
        :rtype: list(BBox)
        """
        bbox_list = self.bbox_list
        if buffer:
            bbox_list = [bbox.buffer(buffer) for bbox in bbox_list]

        if reduce_bbox_sizes is None:
            reduce_bbox_sizes = self.reduce_bbox_sizes
        if reduce_bbox_sizes:
            bbox_list = self._reduce_sizes(bbox_list)

        if crs:
            return [bbox.transform(crs) for bbox in bbox_list]
        return bbox_list

    def get_geometry_list(self):
        """ For each bounding box an intersection with the shape of entire given area is calculated. CRS of the returned
        shapes is the same as CRS of the given area.

        :return: List of polygons or multipolygons corresponding to the order of bounding boxes
        :rtype: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
        """
        return [self._intersection_area(bbox) for bbox in self.bbox_list]

    def get_info_list(self):
        """ Returns a list of dictionaries containing information about bounding boxes obtained in split. The order in
        the list matches the order of the list of bounding boxes.

        :return: List of dictionaries
        :rtype: list(BBox)
        """
        return self.info_list

    def get_area_shape(self):
        """ Returns a single shape of entire area described with `shape_list` parameter

        :return: A multipolygon which is a union of shapes describing the area
        :rtype: shapely.geometry.multipolygon.MultiPolygon
        """
        return self.area_shape

    def get_area_bbox(self, crs=None):
        """ Returns a bounding box of the entire area

        :param crs: Coordinate reference system in which the bounding box should be returned. If `None` the CRS will
            be the default CRS of the splitter.
        :type crs: CRS or None
        :return: A bounding box of the area defined by the `shape_list`
        :rtype: BBox
        """
        bbox_list = [BBox(shape.bounds, crs=self.crs) for shape in self.shape_list]
        area_minx = min([bbox.lower_left[0] for bbox in bbox_list])
        area_miny = min([bbox.lower_left[1] for bbox in bbox_list])
        area_maxx = max([bbox.upper_right[0] for bbox in bbox_list])
        area_maxy = max([bbox.upper_right[1] for bbox in bbox_list])
        bbox = BBox([area_minx, area_miny, area_maxx, area_maxy], crs=self.crs)
        if crs is None:
            return bbox
        return bbox.transform(crs)

    def _intersects_area(self, bbox):
        """ Checks if the bounding box intersects the entire area

        :param bbox: A bounding box
        :type bbox: BBox
        :return: `True` if bbox intersects the entire area else False
        :rtype: bool
        """
        return self._bbox_to_area_polygon(bbox).intersects(self.area_shape)

    def _intersection_area(self, bbox):
        """ Calculates the intersection of a given bounding box and the entire area

        :param bbox: A bounding box
        :type bbox: BBox
        :return: A shape of intersection
        :rtype: shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon
        """
        return self._bbox_to_area_polygon(bbox).intersection(self.area_shape)

    def _bbox_to_area_polygon(self, bbox):
        """ Transforms bounding box into a polygon object in the area CRS.

        :param bbox: A bounding box
        :type bbox: BBox
        :return: A polygon
        :rtype: shapely.geometry.polygon.Polygon
        """
        projected_bbox = bbox.transform(self.crs)
        return projected_bbox.geometry

    def _reduce_sizes(self, bbox_list):
        """ Reduces sizes of bounding boxes
        """
        return [BBox(self._intersection_area(bbox).bounds, self.crs).transform(bbox.crs) for bbox in bbox_list]


class BBoxSplitter(AreaSplitter):
    """ A tool that splits the given area into smaller parts. Given the area it calculates its bounding box and splits
    it into smaller bounding boxes of equal size. Then it filters out the bounding boxes that do not intersect the
    area. If specified by user it can also reduce the sizes of the remaining bounding boxes to best fit the area.

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param split_shape: Parameter that describes the shape in which the area bounding box will be split. It can be a
                        tuple of the form `(n, m)` which means the area bounding box will be split into `n` columns and
                        `m` rows. It can also be a single integer `n` which is the same as `(n, n)`.
    :type split_shape: int or (int, int)
    :param reduce_bbox_sizes: If `True` it will reduce the sizes of bounding boxes so that they will tightly fit
        the given area geometry from `shape_list`.
    :type reduce_bbox_sizes: bool
    """
    def __init__(self, shape_list, crs, split_shape, **kwargs):
        super().__init__(shape_list, crs, **kwargs)

        self.split_shape = self._parse_split_shape(split_shape)

        self._make_split()

    @staticmethod
    def _parse_split_shape(split_shape):
        """ Parses the parameter `split_shape`

        :param split_shape: The parameter `split_shape` from class initialization
        :type split_shape: int or (int, int)
        :return: A tuple of n
        :rtype: (int, int)
        :raises: ValueError
        """
        if isinstance(split_shape, int):
            return split_shape, split_shape
        if isinstance(split_shape, (tuple, list)):
            if len(split_shape) == 2 and isinstance(split_shape[0], int) and isinstance(split_shape[1], int):
                return split_shape[0], split_shape[1]
            raise ValueError("Content of split_shape {} must be 2 integers.".format(split_shape))
        raise ValueError("Split shape must be an int or a tuple of 2 integers.")

    def _make_split(self):
        """ This method makes the split
        """
        columns, rows = self.split_shape
        bbox_partition = self.area_bbox.get_partition(num_x=columns, num_y=rows)

        self.bbox_list = []
        self.info_list = []
        for i, j in itertools.product(range(columns), range(rows)):
            if self._intersects_area(bbox_partition[i][j]):
                self.bbox_list.append(bbox_partition[i][j])

                info = {'parent_bbox': self.area_bbox,
                        'index_x': i,
                        'index_y': j}
                self.info_list.append(info)


class OsmSplitter(AreaSplitter):
    """ A tool that splits the given area into smaller parts. For the splitting it uses Open Street Map (OSM) grid on
    the specified zoom level. It calculates bounding boxes of all OSM tiles that intersect the area. If specified by
    user it can also reduce the sizes of the remaining bounding boxes to best fit the area.

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param zoom_level: A zoom level defined by OSM. Level 0 is entire world, level 1 splits the world into 4 parts, etc.
    :type zoom_level: int
    :param reduce_bbox_sizes: If `True` it will reduce the sizes of bounding boxes so that they will tightly fit
        the given area geometry from `shape_list`.
    :type reduce_bbox_sizes: bool
    """
    POP_WEB_MAX = transform_point((180, 0), CRS.WGS84, CRS.POP_WEB)[0]

    def __init__(self, shape_list, crs, zoom_level, **kwargs):
        super().__init__(shape_list, crs, **kwargs)

        self.zoom_level = zoom_level

        self._make_split()

    def _make_split(self, ):
        """This method makes the split
        """
        self.area_bbox = self.get_area_bbox(CRS.POP_WEB)
        self._check_area_bbox()

        self.bbox_list = []
        self.info_list = []
        self._recursive_split(self.get_world_bbox(), 0, 0, 0)

        for i, bbox in enumerate(self.bbox_list):
            self.bbox_list[i] = bbox.transform(self.crs)

    def _check_area_bbox(self):
        """ The method checks if the area bounding box is completely inside the OSM grid. That means that its latitudes
        must be contained in the interval (-85.0511, 85.0511)

        :raises: ValueError
        """
        for coord in self.area_bbox:
            if abs(coord) > self.POP_WEB_MAX:
                raise ValueError('OsmTileSplitter only works for areas which have latitude in interval '
                                 '(-85.0511, 85.0511)')

    def get_world_bbox(self):
        """ Creates a bounding box of the entire world in EPSG: 3857

        :return: Bounding box of entire world
        :rtype: BBox
        """
        return BBox((-self.POP_WEB_MAX, -self.POP_WEB_MAX, self.POP_WEB_MAX, self.POP_WEB_MAX), crs=CRS.POP_WEB)

    def _recursive_split(self, bbox, zoom_level, column, row):
        """ Method that recursively creates bounding boxes of OSM grid that intersect the area.

        :param bbox: Bounding box
        :type bbox: BBox
        :param zoom_level: OSM zoom level
        :type zoom_level: int
        :param column: Column in the OSM grid
        :type column: int
        :param row: Row in the OSM grid
        :type row: int
        """
        if zoom_level == self.zoom_level:
            self.bbox_list.append(bbox)
            self.info_list.append({'zoom_level': zoom_level,
                                   'index_x': column,
                                   'index_y': row})
            return

        bbox_partition = bbox.get_partition(num_x=2, num_y=2)
        for i, j in itertools.product(range(2), range(2)):
            if self._intersects_area(bbox_partition[i][j]):
                self._recursive_split(bbox_partition[i][j], zoom_level + 1, 2 * column + i, 2 * row + 1 - j)


class TileSplitter(AreaSplitter):
    """ A tool that splits the given area into smaller parts. Given the area, time interval and data source it collects
    info from Sentinel Hub WFS service about all satellite tiles intersecting the area. For each of them it calculates
    bounding box and if specified it splits these bounding boxes into smaller bounding boxes. Then it filters out the
    ones that do not intersect the area. If specified by user it can also reduce the sizes of the remaining bounding
    boxes to best fit the area.

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param time_interval: Interval with start and end date of the form YYYY-MM-DDThh:mm:ss or YYYY-MM-DD
    :type time_interval: (str, str)
    :param tile_split_shape: Parameter that describes the shape in which the satellite tile bounding boxes will be
                             split. It can be a tuple of the form `(n, m)` which means the tile bounding boxes will be
                             split into `n` columns and `m` rows. It can also be a single integer `n` which is the same
                             as `(n, n)`.
    :type split_shape: int or (int, int)
    :param data_source: Source of requested satellite data. Default is Sentinel-2 L1C data.
    :type data_source: sentinelhub.constants.DataSource
    :param instance_id: User's Sentinel Hub instance id. If `None` the instance id is taken from the ``config.json``
                        configuration file.
    :type instance_id: str
    :param reduce_bbox_sizes: If `True` it will reduce the sizes of bounding boxes so that they will tightly fit the
        given area geometry from `shape_list`.
    :type reduce_bbox_sizes: bool
    """
    def __init__(self, shape_list, crs, time_interval, tile_split_shape=1, data_source=DataSource.SENTINEL2_L1C,
                 instance_id=None, **kwargs):
        super().__init__(shape_list, crs, **kwargs)

        if data_source is DataSource.DEM:
            raise ValueError('This splitter does not support splitting area by DEM tiles. Please specify some other '
                             'DataSource')

        self.time_interval = time_interval
        self.tile_split_shape = tile_split_shape
        self.data_source = data_source
        self.instance_id = instance_id

        self.tile_dict = None

        self._make_split()

    def _make_split(self):
        """ This method makes the split
        """
        self.tile_dict = {}

        wfs = WebFeatureService(self.area_bbox, self.time_interval, data_source=self.data_source,
                                instance_id=self.instance_id)
        date_list = wfs.get_dates()
        geometry_list = wfs.get_geometries()
        for tile_info, (date, geometry) in zip(wfs, zip(date_list, geometry_list)):
            tile_name = ''.join(tile_info['properties']['path'].split('/')[4:7])
            if tile_name not in self.tile_dict:
                self.tile_dict[tile_name] = {'bbox': BBox(tile_info['properties']['mbr'],
                                                          crs=tile_info['properties']['crs']),
                                             'times': [],
                                             'geometries': []}
            self.tile_dict[tile_name]['times'].append(date)
            self.tile_dict[tile_name]['geometries'].append(geometry)

        self.tile_dict = {tile_name: tile_props for tile_name, tile_props in self.tile_dict.items() if
                          self._intersects_area(tile_props['bbox'])}

        self.bbox_list = []
        self.info_list = []

        for tile_name, tile_info in self.tile_dict.items():
            tile_bbox = tile_info['bbox']
            bbox_splitter = BBoxSplitter([tile_bbox.geometry], tile_bbox.crs,
                                         split_shape=self.tile_split_shape)

            for bbox, info in zip(bbox_splitter.get_bbox_list(), bbox_splitter.get_info_list()):
                if self._intersects_area(bbox):
                    info['tile'] = tile_name

                    self.bbox_list.append(bbox)
                    self.info_list.append(info)

    def get_tile_dict(self):
        """ Returns the dictionary of satellite tiles intersecting the area geometry. For each tile they contain info
        about their bounding box and lists of acquisitions and geometries

        :return: Dictionary containing info about tiles intersecting the area
        :rtype: dict
        """
        return self.tile_dict


class CustomGridSplitter(AreaSplitter):
    """ Splitting class which can split according to given custom collection of bounding boxes

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param bbox_grid: A collection of bounding boxes defining a grid of splitting. All of them have to be in the same
        CRS.
    :type bbox_grid: list(BBox) or BBoxCollection
    :param bbox_split_shape: Parameter that describes the shape in which each of the bounding boxes in the given grid
        will be split. It can be a tuple of the form `(n, m)` which means the tile bounding boxes will be
        split into `n` columns and `m` rows. It can also be a single integer `n` which is the same as `(n, n)`.
    :type bbox_split_shape: int or (int, int)
    :param reduce_bbox_sizes: If `True` it will reduce the sizes of bounding boxes so that they will tightly fit
        the given geometry in `shape_list`.
    :type reduce_bbox_sizes: bool
    """
    def __init__(self, shape_list, crs, bbox_grid, bbox_split_shape=1, **kwargs):
        super().__init__(shape_list, crs, **kwargs)

        self.bbox_grid = self._parse_bbox_grid(bbox_grid)
        self.bbox_split_shape = bbox_split_shape

        self._make_split()

    @staticmethod
    def _parse_bbox_grid(bbox_grid):
        """ Helper method for parsing bounding box grid. It will try to parse it into `BBoxCollection`
        """
        if isinstance(bbox_grid, BBoxCollection):
            return bbox_grid

        if isinstance(bbox_grid, list):
            return BBoxCollection(bbox_grid)

        raise ValueError("Parameter 'bbox_grid' should be an instance of {}".format(BBoxCollection.__name__))

    def _make_split(self):
        """ This method makes the split
        """
        self.bbox_list = []
        self.info_list = []

        for grid_idx, grid_bbox in enumerate(self.bbox_grid):
            if self._intersects_area(grid_bbox):

                bbox_splitter = BBoxSplitter([grid_bbox.geometry], grid_bbox.crs,
                                             split_shape=self.bbox_split_shape)

                for bbox, info in zip(bbox_splitter.get_bbox_list(), bbox_splitter.get_info_list()):
                    if self._intersects_area(bbox):
                        info['grid_index'] = grid_idx

                        self.bbox_list.append(bbox)
                        self.info_list.append(info)


class UTMGridSplitter(AreaSplitter):
    """ Splitter that returns bboxes of fixed size aligned to UTM grid tiles as defined by the MGRS

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param bbox_size: Physical size in metres of generated bounding boxes. Could be a float or tuple of floats
    :type bbox_size: float or (float, float)
    """
    def __init__(self, shape_list, crs, bbox_size):
        super(UTMGridSplitter, self).__init__(shape_list, crs)

        self.bbox_size = self._parse_bbox_size(bbox_size)

        self.utm_grid = self._get_overlapping_utm_grid()

        self._make_split()

    @staticmethod
    def _parse_bbox_size(bbox_size):
        """ Parses the parameter `bbox_size`. The parameter defines the physical size of BBoxes

        :param bbox_size: The parameter `bbox_size` from class initialization
        :type bbox_size: int or float or (int, int) or (float, float)
        :return: A tuple of ints or floats specifying physical size of bounding boxes
        :rtype: (int, int) or (float, float)
        :raises: ValueError
        """
        if isinstance(bbox_size, (int, float)):
            return bbox_size, bbox_size
        if isinstance(bbox_size, (tuple, list)):
            if len(bbox_size) == 2 and all(isinstance(bbox_dim, (float, int)) for bbox_dim in bbox_size):
                return bbox_size[0], bbox_size[1]
            raise ValueError('Content of bbox_size {} must be 2 ints or 2 floats.'.format(bbox_size))
        raise ValueError('BBox size must be an int or float or a tuple of 2 ints or floats.')

    def _get_overlapping_utm_grid(self):
        """ Find UTM grid zones overlapping with input area shape

        :return: List of geometries and properties of UTM grid zones overlapping with input area shape
        """
        # file downloaded from faculty.baruch.cuny.edu/geoportal/data/esri/world/utmzone.zip
        utm_grid_filename = os.path.join(os.path.dirname(__file__), '.utmzones.geojson')

        if not os.path.isfile(utm_grid_filename):
            raise IOError('UTM grid definition file does not exist: %s' % os.path.abspath(utm_grid_filename))

        with open(utm_grid_filename) as utm_grid_file:
            utm_grid = json.load(utm_grid_file)['features']

        for utm_zone in utm_grid:
            utm_zone['geometry']['coordinates'] = np.round(np.array(utm_zone['geometry']['coordinates']), 4).tolist()

        utm_geom_list = GeometryCollection([shape(utm_zone['geometry']) for utm_zone in utm_grid])
        utm_prop_list = [utm_zone['properties'] for utm_zone in utm_grid]

        return [(utm_geom, utm_prop) for utm_geom, utm_prop in zip(utm_geom_list, utm_prop_list)
                if utm_geom.intersects(self.area_bbox.transform(CRS.WGS84).geometry)]

    @staticmethod
    def _get_utm_from_props(utm_dict):
        """ Return the UTM CRS corresponding to the UTM described by the properties dictionary

        :param utm_dict: Dictionary reporting name of the UTM zone and MGRS grid
        :type utm_dict: dict
        :return: UTM coordinate reference system
        :rtype: sentinelhub.CRS
        """
        return CRS(int('32{}{}'.format(6 if utm_dict['ROW_'] >= 'N' else 7, str(utm_dict['ZONE']).zfill(2))))

    def _make_split(self):
        """ Split each UTM grid into equally sized bboxes in correct UTM zone
        """
        size_x, size_y = self.bbox_size
        self.bbox_list = []
        self.info_list = []

        shape_geometry = Geometry(self.area_shape, self.crs)

        index = 0

        for utm_cell in self.utm_grid:
            utm_cell_geom, utm_cell_prop = utm_cell
            utm_crs = self._get_utm_from_props(utm_cell_prop)

            bbox_utm = Geometry(utm_cell_geom, crs=CRS.WGS84).transform(utm_crs).bbox
            bbox_partition = bbox_utm.get_partition(size_x=size_x, size_y=size_y)

            intersections = utm_cell_geom.intersection(shape_geometry.transform(CRS.WGS84).geometry)
            if isinstance(intersections, (Polygon, MultiPolygon)):
                intersections = [Geometry(intersections, CRS.WGS84).transform(utm_crs)]
            elif isinstance(intersections, GeometryCollection):
                intersections = [Geometry(intersection, CRS.WGS84).transform(utm_crs) for intersection in intersections]

            columns, rows = len(bbox_partition), len(bbox_partition[0])
            for i, j in itertools.product(range(columns), range(rows)):
                if any(bbox_partition[i][j].geometry.intersects(intersection.geometry)
                       for intersection in intersections):
                    self.bbox_list.append(bbox_partition[i][j])
                    self.info_list.append(dict(crs=utm_crs.name,
                                               utm=str(utm_cell_prop['ZONE']).zfill(2)+'_'+utm_cell_prop['ROW_'],
                                               index=index,
                                               index_x=i,
                                               index_y=j))
                    index += 1

    def get_bbox_list(self, buffer=None, *args):
        """ Get list of bounding boxes.

        The CRS is fixed to the computed UTM CRS. This BBox splitter does not support reducing size of output
        bounding boxes

        :param buffer: A percentage of each BBox size increase. This will cause neighbouring bounding boxes to overlap.
        :type buffer: float or None
        :return: List of bounding boxes
        :rtype: list(BBox)
        """
        return super(UTMGridSplitter, self).get_bbox_list(buffer=buffer)


class UTMZoneSplitter(UTMGridSplitter):
    """ Splitter that returns bounding boxes of fixed size aligned to the equator and the UTM zones.

    This splitter does not consider UTM rows defined by the Military Grid Reference System, as opposed to the
    `UTMGridSplitter`

    :param shape_list: A list of geometrical shapes describing the area of interest
    :type shape_list: list(shapely.geometry.multipolygon.MultiPolygon or shapely.geometry.polygon.Polygon)
    :param crs: Coordinate reference system of the shapes in `shape_list`
    :type crs: CRS
    :param bbox_size: Physical size in metres of generated bounding boxes. Could be a float or tuple of floats
    :type bbox_size: float or (float, float)
    """
    LNG_MIN, LNG_MAX, LNG_UTM = -180, 180, 6
    LAT_MIN, LAT_MAX, LAT_EQ = -80, 84, 0

    def __init__(self, shape_list, crs, bbox_size):
        super(UTMZoneSplitter, self).__init__(shape_list, crs, bbox_size)

    def _get_overlapping_utm_grid(self):
        """ Find UTM zones overlapping with input area shape

        The returned geometry corresponds to the a triangle ranging from the equator to the north/south pole

        :return: List of geometries and properties of UTM zones overlapping with input area shape
        """
        utm_geom_list = [Polygon([(lng, lat[0]), (lng, lat[1]), (lng + self.LNG_UTM, lat[0]),
                                  (lng + self.LNG_UTM, lat[1]), (lng, lat[0])])
                         for lat in [(self.LAT_EQ, self.LAT_MAX), (self.LAT_MIN, self.LAT_EQ)]
                         for lng in range(self.LNG_MIN, self.LNG_MAX, self.LNG_UTM)]
        utm_prop_list = [dict(ZONE=zone, ROW_=direction) for direction in ['N', 'S'] for zone in range(1, 61)]

        return [(utm_geom.convex_hull, utm_prop) for utm_geom, utm_prop in zip(utm_geom_list, utm_prop_list)
                if utm_geom.intersects(self.area_bbox.transform(CRS.WGS84).geometry)]

    @staticmethod
    def _get_utm_from_props(utm_dict):
        """ Return the UTM CRS corresponding to the UTM described by the properties dictionary

        :param utm_dict: Dictionary reporting name of the UTM zone and MGRS grid
        :type utm_dict: dict
        :return: UTM coordinate reference system
        :rtype: sentinelhub.CRS
        """
        return CRS(int('32{}{}'.format(6 if utm_dict['ROW_'] == 'N' else 7, str(utm_dict['ZONE']).zfill(2))))
