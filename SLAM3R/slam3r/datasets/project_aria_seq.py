# Copyright (C) 2024-present Naver Corporation. All rights reserved.
# Licensed under CC BY-NC-SA 4.0 (non-commercial use only).
#
# --------------------------------------------------------
# Dataloader for preprocessed project-aria dataset
# --------------------------------------------------------
import os.path as osp
import os
import cv2
import numpy as np
import math

SLAM3R_DIR = osp.dirname(osp.dirname(osp.dirname(osp.abspath(__file__))))
import sys # noqa: E402
sys.path.insert(0, SLAM3R_DIR) # noqa: E402
from slam3r.datasets.base.base_stereo_view_dataset import BaseStereoViewDataset
from slam3r.utils.image import imread_cv2    


class Aria_Seq(BaseStereoViewDataset):
    def __init__(self,  
                 ROOT='data/projectaria_ase_processed', 
                 num_views=2, 
                 scene_name=None,
                 sample_freq=1,
                 start_freq=1,
                 filter=False,
                 rand_sel=False,
                 winsize=0, 
                 sel_num=0,
                 *args,**kwargs):
        super().__init__(*args, **kwargs)
        self.ROOT = ROOT
        self.sample_freq = sample_freq
        self.start_freq = start_freq
        self.num_views = num_views
        
        self.rand_sel = rand_sel 
        if rand_sel:
            assert winsize > 0 and sel_num > 0
            comb_num = math.comb(winsize-1, num_views-2)
            assert comb_num >= sel_num
            self.winsize = winsize
            self.sel_num = sel_num
        else:
            self.winsize = sample_freq*(num_views-1)
        
        self.scene_names = os.listdir(self.ROOT)
        #按照scene_name转化为int排序
        self.scene_names = [int(scene_name) for scene_name in self.scene_names if scene_name.isdigit()]
        self.scene_names = sorted(self.scene_names)
        self.scene_names = [str(scene_name) for scene_name in self.scene_names]
        total_scene_num = len(self.scene_names)
        
        if self.split == 'train':
            #挑选十分之九的数据作为训练集
            self.scene_names = self.scene_names[:int(total_scene_num*0.9)]
        elif self.split=='test':
            self.scene_names = self.scene_names[int(total_scene_num*0.9):]
        if scene_name is not None:
            assert self.split is None
            if isinstance(scene_name, list): 
                self.scene_names = scene_name
            else:
                if isinstance(scene_name, int):
                    scene_name = str(scene_name)
                assert isinstance(scene_name, str)
                self.scene_names = [scene_name]
                
        self._load_data(filter=filter)
        print(self)

    def filter_windows(self, sid, eid, image_names):
        return False

    def _load_data(self, filter=False):
        self.sceneids = []  
        self.images = []
        self.intrinsics = []   #scene_num*(3,3)
        # self.trajectories = []  #c2w  (4,4)
        self.win_bid = []

        num_count = 0
        for id, scene_name in enumerate(self.scene_names):
            scene_dir = os.path.join(self.ROOT, scene_name)
            # print(id, scene_name)
            image_names = os.listdir(os.path.join(scene_dir, 'color'))
            image_names = sorted(image_names)
            intrinsic = np.loadtxt(os.path.join(scene_dir, 'intrinsic', 'intrinsic_color.txt'))[:3,:3]
            image_num = len(image_names)
            # precompute the window indices
            for i in range(0, image_num, self.start_freq):
                last_id = i+self.winsize
                if last_id >= image_num:
                    break
                if filter and self.filter_windows(i, last_id, image_names):
                    continue
                self.win_bid.append((num_count+i, num_count+last_id))
            
            self.intrinsics.append(intrinsic)
            self.images +=  image_names
            self.sceneids += [id,] * image_num
            num_count += image_num
        # print(self.sceneids, self.scene_names)    
        self.intrinsics = np.stack(self.intrinsics, axis=0)
        print(self.intrinsics.shape)
        assert len(self.sceneids)==len(self.images), f"{len(self.sceneids)}, {len(self.images)}"   
        
    def __len__(self):
        if self.rand_sel:
            return self.sel_num*len(self.win_bid)
        return len(self.win_bid)    
    
    def get_img_idxes(self, idx, rng):
        if self.rand_sel:
            sid, eid = self.win_bid[idx//self.sel_num]
            if idx % self.sel_num == 0:
                #生成sid与eid之间的均匀采样
                return np.linspace(sid, eid, self.num_views, endpoint=True, dtype=int)
                
            #首尾必须选择，中间随机选择n-2个
            if self.num_views == 2:
                return [sid, eid]
            sel_ids = rng.choice(range(sid+1, eid), self.num_views-2, replace=False)
            sel_ids.sort()
            return [sid] + list(sel_ids) + [eid]
        else:
            sid, eid = self.win_bid[idx]
            return [sid + i*self.sample_freq for i in range(self.num_views)]
            
    
    def _get_views(self, idx, resolution, rng):

        image_idxes = self.get_img_idxes(idx, rng)
        # print(image_idxes)
        views = []
        for view_idx in image_idxes:
            scene_id = self.sceneids[view_idx]
            scene_dir = osp.join(self.ROOT, self.scene_names[scene_id])

            intrinsics = self.intrinsics[scene_id]
            basename = self.images[view_idx]
            camera_pose = np.loadtxt(osp.join(scene_dir, 'pose', basename.replace('.jpg', '.txt')))
            # Load RGB image
            rgb_image = imread_cv2(osp.join(scene_dir, 'color', basename))
            # Load depthmap
            depthmap = imread_cv2(osp.join(scene_dir, 'depth', basename.replace('.jpg', '.png')), cv2.IMREAD_UNCHANGED)
            depthmap = depthmap.astype(np.float32) / 1000
            depthmap[~np.isfinite(depthmap)] = 0  # invalid
            depthmap[depthmap > 20] = 0  # invalid
            
            rgb_image, depthmap, intrinsics = self._crop_resize_if_necessary(
                rgb_image, depthmap, intrinsics, resolution, rng=rng, info=view_idx)

            views.append(dict(
                img=rgb_image,
                depthmap=depthmap.astype(np.float32),
                camera_pose=camera_pose.astype(np.float32),
                camera_intrinsics=intrinsics.astype(np.float32),
                dataset='Aria',
                label=self.scene_names[scene_id] + '_' + basename,
                instance=f'{str(idx)}_{str(view_idx)}',
            ))
        # print([view['label'] for view in views])
        return views  

if __name__ == "__main__":
    from slam3r.datasets.base.base_stereo_view_dataset import view_name
    from slam3r.viz import SceneViz, auto_cam_size
    from slam3r.utils.image import rgb
    import trimesh

    num_views = 4
    # dataset = Aria_Seq(resolution=(224,224), 
    #                              num_views=4,
    #                              start_freq=1, sample_freq=2)
    dataset = Aria_Seq(split='train', resolution=(224,224), 
                                 num_views=num_views,
                                 start_freq=1, rand_sel=True, winsize=6, sel_num=3)
    save_dir = "visualization/aria_seq_views"
    os.makedirs(save_dir, exist_ok=True)

    for idx in np.random.permutation(len(dataset))[:10]:
    # for idx in range(len(dataset))[5:10000:2000]:
        os.makedirs(osp.join(save_dir, str(idx)), exist_ok=True)
        views = dataset[(idx,0)]
        assert len(views) == num_views
        all_pts = []
        all_color=[]
        for i, view in enumerate(views):
            img = np.array(view['img']).transpose(1, 2, 0)
            # save_path = osp.join(save_dir, str(idx), f"{'_'.join(view_name(view).split('/')[1:])}.jpg")
            save_path = osp.join(save_dir, str(idx), f"{i}_{view['label']}")
            # img=cv2.COLOR_RGB2BGR(img)
            img=img[...,::-1]
            img = (img+1)/2
            cv2.imwrite(save_path, img*255)
            print(f"save to {save_path}")
            pts3d = np.array(view['pts3d']).reshape(-1,3)
            pct = trimesh.PointCloud(pts3d, colors=img.reshape(-1, 3))
            pct.export(save_path.replace('.jpg','.ply'))
            all_pts.append(pts3d)
            all_color.append(img.reshape(-1, 3))
        all_pts = np.concatenate(all_pts, axis=0)
        all_color = np.concatenate(all_color, axis=0)
        pct = trimesh.PointCloud(all_pts, all_color)
        pct.export(osp.join(save_dir, str(idx), f"all.ply"))
    # for idx in range(len(dataset)):
    #     views = dataset[(idx,0)]
    #     print([view['label'] for view in views])