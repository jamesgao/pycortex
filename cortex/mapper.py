import os
import warnings
from itertools import product
from collections import Counter

import nibabel
import numpy as np
from scipy import sparse, spatial

warnings.simplefilter('ignore', sparse.SparseEfficiencyWarning)

from . import polyutils
from .db import surfs

class Mapper(object):
    '''Maps data from epi volume onto surface using various projections'''
    def __init__(self, subject, xfmname, recache=False, **kwargs):
        self.idxmap = None
        self.subject, self.xfmname = subject, xfmname
        fnames = surfs.getFiles(subject)
        ptype = self.__class__.__name__.lower()
        kwds ='_'.join(['%s%s'%(k,str(v)) for k, v in list(kwargs.items())])
        if len(kwds) > 0:
            ptype += '_'+kwds
        self.cachefile = fnames['projcache'].format(xfmname=xfmname, projection=ptype)

        xfm, epifile = surfs.getXfm(subject, xfmname)
        nib = nibabel.load(epifile)
        self.shape = nib.get_shape()[:3][::-1]

        xfmfile = fnames['xfms'].format(xfmname=xfmname)
        try:
            npz = np.load(self.cachefile)
            if recache or os.stat(self.cachefile).st_mtime < os.stat(xfmfile).st_mtime:
                raise IOError

            left = (npz['left_data'], npz['left_indices'], npz['left_indptr'])
            right = (npz['right_data'], npz['right_indices'], npz['right_indptr'])
            lsparse = sparse.csr_matrix(left, shape=npz['left_shape'])
            rsparse = sparse.csr_matrix(right, shape=npz['right_shape'])
            self.masks = [lsparse, rsparse]
            self.nverts = lsparse.shape[0] + rsparse.shape[0]
        except IOError:
            self._recache(subject, xfmname, **kwargs)

    @property
    def mask(self):
        mask = np.array(self.masks[0].sum(0) + self.masks[1].sum(0))
        return (mask.squeeze() != 0).reshape(self.shape)

    @property
    def hemimasks(self):
        func = lambda m: (np.array(m.sum(0)).squeeze() != 0).reshape(self.shape)
        return list(map(func, self.masks))

    def __repr__(self):
        ptype = self.__class__.__name__
        return '<%s mapper for (%s, %s) with %d vertices>'%(ptype, self.subject, self.xfmname, self.nverts)

    def __call__(self, data):
        if self.nverts in data.shape:
            llen = self.masks[0].shape[0]
            left, right = data[..., :llen], data[..., llen:]

            if self.idxmap is not None:
                return left[..., self.idxmap[0]], right[..., self.idxmap[1]]
            return left, right
            

        if data.ndim in (1, 3):
            data = data[np.newaxis]

        mapped = []
        for mask in self.masks:
            if self.mask.sum() in data.shape:
                shape = (np.prod(self.shape), data.shape[0])
                norm = np.zeros(shape)
                norm[self.mask.ravel()] = data.T
            elif data.ndim == 4:
                norm = data.reshape(len(data), -1).T
            else:
                raise ValueError

            mapped.append(np.array(mask * norm).T.squeeze())

        if self.idxmap is not None:
            mapped[0] = mapped[0][..., self.idxmap[0]]
            mapped[1] = mapped[1][..., self.idxmap[1]]

        return mapped
        
    def backwards(self, verts):
        '''Projects vertex data back into volume space

        Parameters
        ----------
        verts : array_like
            If uint array and max <= nverts, assume binary mask of vertices
            If float array and len == nverts, project float values into volume
        '''
        left = np.zeros((self.masks[0].shape[0],), dtype=bool)
        right = np.zeros((self.masks[1].shape[0],), dtype=bool)
        if isinstance(verts, (list, tuple)) and len(verts) == 2:
            if len(verts[0]) == len(left):
                left = verts[0]
                right = verts[1]
            elif verts[0].max() < len(left):
                left[verts[0]] = True
                right[verts[1]] = True
            else:
                raise ValueError
        else:
            if len(verts) == self.nverts:
                left = verts[:len(left)]
                right = verts[len(left):]
            elif verts.max() < self.nverts:
                left[verts[verts < len(left)]] = True
                right[verts[verts >= len(left)] - len(left)] = True
            else:
                raise ValueError

        output = []
        for mask, data in zip(self.masks, [left, right]):
            proj = data * mask
            output.append(np.array(proj).reshape(self.shape))

        return output

    def _recache(self, subject, xfmname, **kwargs):
        masks = []
        coord, epifile = surfs.getXfm(subject, xfmname, xfmtype='coord')
        fid = surfs.getVTK(subject, 'fiducial', merge=False, nudge=False)
        #flat = surfs.getVTK(subject, 'flat', merge=False, nudge=False)

        for pts, polys, _ in fid:
            coords = polyutils.transform(coord, pts)
            masks.append(self._getmask(coords, polys, **kwargs))

        self._savecache(*masks)

    def _savecache(self, left, right):
        self.nverts = left.shape[0] + right.shape[0]
        self.masks = [left, right]
        np.savez(self.cachefile, 
            left_data=left.data, 
            left_indices=left.indices, 
            left_indptr=left.indptr,
            left_shape=left.shape,
            right_data=right.data,
            right_indices=right.indices,
            right_indptr=right.indptr,
            right_shape=right.shape)

class ThickMapper(Mapper):
    def _recache(self, subject, xfmname, **kwargs):
        masks = []
        coord, epifile = surfs.getXfm(subject, xfmname, xfmtype='coord')
        pia = surfs.getVTK(subject, "pia", merge=False, nudge=False)
        wm = surfs.getVTK(subject, "wm", merge=False, nudge=False)
        #flat = surfs.getVTK(subject, "flat", merge=False, nudge=False)
        
        #iterate over hemispheres
        for (wpts, polys, _), (ppts, _, _) in zip(pia, wm):
            tpia = polyutils.transform(coord, ppts)
            twm = polyutils.transform(coord, wpts)
            masks.append(self._getmask(tpia, twm, polys, **kwargs))
            
        self._savecache(*masks)

class Nearest(Mapper):
    '''Maps epi volume data to surface using nearest neighbor interpolation'''
    def _getmask(self, coords, polys):
        valid = np.ones((len(coords),), dtype=bool)

        coords = np.where(np.mod(coords, 2) == 0.5, np.ceil(coords), np.around(coords)).astype(int)
        d1 = np.logical_and(0 <= coords[:,0], coords[:,0] < self.shape[2])
        d2 = np.logical_and(0 <= coords[:,1], coords[:,1] < self.shape[1])
        d3 = np.logical_and(0 <= coords[:,2], coords[:,2] < self.shape[0])
        valid = np.logical_and(np.logical_and(valid, d1), np.logical_and(d2, d3))

        ravelidx = np.ravel_multi_index(coords.T[::-1], self.shape, mode='clip')

        ij = np.array([np.nonzero(valid)[0], ravelidx[valid]])
        data = np.ones((len(ij.T),), dtype=bool)
        csrshape = len(coords), np.prod(self.shape)
        return sparse.csr_matrix((data, ij), dtype=bool, shape=csrshape)

class Trilinear(Mapper):
    def _getmask(self, coords, polys):
        #trilinear interpolation equation from http://paulbourke.net/miscellaneous/interpolation/
        (x, y, z), floor = np.modf(coords.T)
        floor = floor.astype(int)
        ceil = floor + 1
        x[x < 0] = 0
        y[y < 0] = 0
        z[z < 0] = 0

        i000 = np.ravel_multi_index((floor[2], floor[1], floor[0]), self.shape, mode='clip')
        i100 = np.ravel_multi_index((floor[2], floor[1],  ceil[0]), self.shape, mode='clip')
        i010 = np.ravel_multi_index((floor[2],  ceil[1], floor[0]), self.shape, mode='clip')
        i001 = np.ravel_multi_index(( ceil[2], floor[1], floor[0]), self.shape, mode='clip')
        i101 = np.ravel_multi_index(( ceil[2], floor[1],  ceil[0]), self.shape, mode='clip')
        i011 = np.ravel_multi_index(( ceil[2],  ceil[1], floor[0]), self.shape, mode='clip')
        i110 = np.ravel_multi_index((floor[2],  ceil[1],  ceil[0]), self.shape, mode='clip')
        i111 = np.ravel_multi_index(( ceil[2],  ceil[1],  ceil[0]), self.shape, mode='clip')

        v000 = (1-x)*(1-y)*(1-z)
        v100 = x*(1-y)*(1-z)
        v010 = (1-x)*y*(1-z)
        v110 = x*y*(1-z)
        v001 = (1-x)*(1-y)*z
        v101 = x*(1-y)*z
        v011 = (1-x)*y*z
        v111 = x*y*z

        i    = np.tile(np.arange(len(coords)), [8, 1]).T.ravel()
        j    = np.vstack([i000, i100, i010, i001, i101, i011, i110, i111]).T.ravel()
        data = np.vstack([v000, v100, v010, v001, v101, v011, v110, v111]).T.ravel()
        csrshape = len(coords), np.prod(self.shape)
        return sparse.csr_matrix((data, (i, j)), shape=csrshape)

class Lanczos(Mapper):
    def _getmask(self, coords, polys, window=3, renorm=True):
        nZ, nY, nX = self.shape
        dx = coords[:,0] - np.atleast_2d(np.arange(nX)).T
        dy = coords[:,1] - np.atleast_2d(np.arange(nY)).T
        dz = coords[:,2] - np.atleast_2d(np.arange(nZ)).T

        def lanczos(x):
            out = np.zeros_like(x)
            sel = np.abs(x)<window
            selx = x[sel]
            out[sel] = np.sin(np.pi * selx) * np.sin(np.pi * selx / window) * (window / (np.pi**2 * selx**2))
            return out

        Lx = lanczos(dx)
        Ly = lanczos(dy)
        Lz = lanczos(dz)

        import ipdb
        ipdb.set_trace()
        
        mask = sparse.lil_matrix((len(coords), np.prod(self.shape)))
        for v in range(len(coords)):
            ix = np.nonzero(Lx[:,v])[0]
            iy = np.nonzero(Ly[:,v])[0]
            iz = np.nonzero(Lz[:,v])[0]

            vx = Lx[ix,v]
            vy = Ly[iy,v]
            vz = Lz[iz,v]
            try:
                inds = np.ravel_multi_index(np.array(list(product(iz, iy, ix))).T, self.shape)
                vals = np.prod(np.array(list(product(vz, vy, vx))), 1)
                if renorm:
                    vals /= vals.sum()
                mask[v,inds] = vals
            except ValueError:
                pass

            if not v % 1000:
                print(v)

        return mask.tocsr()

class Gaussian(Mapper):
    def _recache(self, subject, xfmname, std=2):
        raise NotImplementedError

class Polyhedral(ThickMapper):
    '''Uses an actual (likely concave) polyhedra betwen the pial and white surfaces
    to estimate the thickness'''
    def _getmask(self, pia, wm, polys):
        mask = sparse.csr_matrix((len(wpts), np.prod(self.shape)))

        from tvtk.api import tvtk
        measure = tvtk.MassProperties()
        planes = tvtk.PlaneCollection()
        for norm in np.vstack([-np.eye(3), np.eye(3)]):
            planes.append(tvtk.Plane(normal=norm))
        ccs = tvtk.ClipClosedSurface(clipping_planes=planes)
        feats = tvtk.FeatureEdges(boundary_edges=1, non_manifold_edges=0, manifold_edges=0, feature_edges=0)
        feats.set_input(ccs.output)

        surf = polyutils.Surface(pia, polys)
        for i, (pts, faces) in enumerate(surf.polyhedra(wm)):
            if len(pts) > 0:
                poly = tvtk.PolyData(points=pts, polys=faces)
                measure.set_input(poly)
                measure.update()
                totalvol = measure.volume
                ccs.set_input(poly)
                measure.set_input(ccs.output)

                bmin = pts.min(0).round().astype(int)
                bmax = (pts.max(0).round() + 1).astype(int)
                vidx = np.mgrid[bmin[0]:bmax[0], bmin[1]:bmax[1], bmin[2]:bmax[2]]
                for vox in vidx.reshape(3, -1).T:
                    try:
                        idx = np.ravel_multi_index(vox[::-1], self.shape)
                        for plane, m in zip(planes, [.5, .5, .5, -.5, -.5, -.5]):
                            plane.origin = vox+m

                        ccs.update()
                        if ccs.output.number_of_cells > 2:
                            measure.update()
                            mask[i, idx] = measure.volume
    
                    except ValueError:
                        print('Voxel not in volume: (%d, %d, %d)'%tuple(vox))

                mask.data[mask.indptr[i]:mask.indptr[i+1]] /= mask[i].sum()

        return mask

class ConvexPolyhedra(ThickMapper):
    def _getmask(self, pia, wm, polys, npts=1024):
        rand = np.random.rand(npts, 3)
        mask = sparse.csr_matrix((len(wm), np.prod(self.shape)))

        surf = polyutils.Surface(pia, polys)
        for i, (pts, polys) in enumerate(surf.polyhedra(wm)):
            if len(pts) > 0:
                #generate points within the bounding box
                samples = rand * (pts.max(0) - pts.min(0)) + pts.min(0)
                #check which points are inside the polyhedron
                inside = polyutils.inside_convex_poly(pts)(samples)

                for idx, value in self._sample(samples[inside]):
                    mask[i, idx] = value / float(sum(inside))

            if i % 100 == 0:
                print(i)

        return mask

class ConvexNN(ConvexPolyhedra):
    def _sample(self, pts):
        coords = pts.round().astype(int)[:,::-1]
        d1 = np.logical_and(0 <= coords[:,0], coords[:,0] < self.shape[0])
        d2 = np.logical_and(0 <= coords[:,1], coords[:,1] < self.shape[1])
        d3 = np.logical_and(0 <= coords[:,2], coords[:,2] < self.shape[2])
        valid = np.logical_and(d1, np.logical_and(d2, d3))
        if valid.any():
            idx = np.ravel_multi_index(coords[valid].T, self.shape)
            return Counter(idx).items()
        return []

class ConvexTrilin(ConvexPolyhedra):
    def _sample(self, pts):
        (x, y, z), floor = np.modf(pts.T)
        floor = floor.astype(int)
        ceil = floor + 1
        x[x < 0] = 0
        y[y < 0] = 0
        z[z < 0] = 0

        i000 = np.ravel_multi_index((floor[2], floor[1], floor[0]), self.shape, mode='clip')
        i100 = np.ravel_multi_index((floor[2], floor[1],  ceil[0]), self.shape, mode='clip')
        i010 = np.ravel_multi_index((floor[2],  ceil[1], floor[0]), self.shape, mode='clip')
        i001 = np.ravel_multi_index(( ceil[2], floor[1], floor[0]), self.shape, mode='clip')
        i101 = np.ravel_multi_index(( ceil[2], floor[1],  ceil[0]), self.shape, mode='clip')
        i011 = np.ravel_multi_index(( ceil[2],  ceil[1], floor[0]), self.shape, mode='clip')
        i110 = np.ravel_multi_index((floor[2],  ceil[1],  ceil[0]), self.shape, mode='clip')
        i111 = np.ravel_multi_index(( ceil[2],  ceil[1],  ceil[0]), self.shape, mode='clip')

        v000 = (1-x)*(1-y)*(1-z)
        v100 = x*(1-y)*(1-z)
        v010 = (1-x)*y*(1-z)
        v110 = x*y*(1-z)
        v001 = (1-x)*(1-y)*z
        v101 = x*(1-y)*z
        v011 = (1-x)*y*z
        v111 = x*y*z

        i    = np.tile(np.arange(len(coords)), [8, 1]).T.ravel()
        j    = np.vstack([i000, i100, i010, i001, i101, i011, i110, i111]).T.ravel()
        data = np.vstack([v000, v100, v010, v001, v101, v011, v110, v111]).T.ravel()


class ConvexLanczos(ConvexPolyhedra):
    def _sample(self, pts):
        raise NotImplementedError